"""Parse and validate the written daily plan.

Accepted shapes (one block per line, tolerant about separators):

    16:00-16:40 commute to MNU library
    16:40 - 19:00  work: linear algebra pset ch3
    19:00–19:30 gym            (en dash is fine)
    4pm-4:40pm relax

The plan must tile the whole enforcement window with no gaps and no overlaps.
"Relax" is a legitimate block -- a holiday is planned rest, not absence of a plan.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Order matters: first match wins.
CATEGORY_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("commute", ("commute", "travel", "bus", "metro", "walk to", "drive", "road", "way to")),
    ("work",    ("work", "study", "pset", "problem set", "essay", "read", "code", "project",
                 "library", "lecture", "class", "homework", "hw", "revise", "practice", "deep")),
    ("sport",   ("gym", "sport", "run", "workout", "train", "swim", "football", "lift", "exercise")),
    ("clean",   ("clean", "tidy", "laundry", "dishes", "wash", "chore", "room")),
    ("skincare",("skincare", "skin", "routine", "shower", "teeth", "groom")),
    ("meal",    ("eat", "meal", "dinner", "lunch", "food", "cook", "breakfast")),
    ("sleep",   ("sleep", "bed", "nap", "rest in bed")),
    ("relax",   ("relax", "chill", "free", "break", "leisure", "family", "friends", "homies")),
]

TIME_RE = r"(\d{1,2})\s*[:.]?\s*(\d{2})?\s*(am|pm)?"
LINE_RE = re.compile(
    rf"^\s*[-*•]?\s*{TIME_RE}\s*(?:-|--|–|—|to|until)\s*{TIME_RE}\s*[-–:|]?\s*(.+?)\s*$",
    re.IGNORECASE,
)


@dataclass
class Block:
    start_min: int
    end_min: int
    label: str
    category: str

    def as_tuple(self) -> tuple[int, int, str, str]:
        return (self.start_min, self.end_min, self.label, self.category)

    def hhmm(self) -> str:
        return f"{self.start_min//60:02d}:{self.start_min%60:02d}-{self.end_min//60:02d}:{self.end_min%60:02d}"


class PlanError(ValueError):
    """Raised with a message written to be shown straight to the user."""


def _to_minutes(hour: str, minute: str | None, meridiem: str | None) -> int:
    h = int(hour)
    m = int(minute) if minute else 0
    if meridiem:
        mer = meridiem.lower()
        if mer == "pm" and h != 12:
            h += 12
        elif mer == "am" and h == 12:
            h = 0
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise PlanError(f"'{hour}:{minute or '00'}' isn't a real time.")
    return h * 60 + m


def categorise(label: str) -> str:
    low = label.lower()
    for cat, words in CATEGORY_KEYWORDS:
        if any(w in low for w in words):
            return cat
    return "other"


def parse(text: str) -> list[Block]:
    blocks: list[Block] = []
    bad: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.lower().startswith("/plan"):
            continue
        m = LINE_RE.match(line)
        if not m:
            bad.append(line)
            continue
        sh, sm, smer, eh, em, emer, label = m.groups()
        start = _to_minutes(sh, sm, smer)
        end = _to_minutes(eh, em, emer)
        if end <= start:
            raise PlanError(f"'{line}' ends before it starts.")
        label = label.strip(" -–—:|")
        if not label:
            raise PlanError(f"'{line}' has times but no task.")
        blocks.append(Block(start, end, label, categorise(label)))

    if bad:
        joined = "\n".join(f"  • {b}" for b in bad[:5])
        raise PlanError(
            "I couldn't read these lines:\n" + joined +
            "\n\nUse: HH:MM-HH:MM what you're doing"
        )
    if not blocks:
        raise PlanError("Empty plan. Send at least one line like `16:00-17:00 commute`.")
    blocks.sort(key=lambda b: b.start_min)
    return blocks


def validate_coverage(blocks: list[Block], window_start: int, window_end: int) -> None:
    """The plan must tile [window_start, window_end) exactly."""
    def fmt(mins: int) -> str:
        return f"{mins//60:02d}:{mins%60:02d}"

    for a, b in zip(blocks, blocks[1:]):
        if b.start_min < a.end_min:
            raise PlanError(f"{a.hhmm()} and {b.hhmm()} overlap. Blocks can't run at the same time.")

    covering = [b for b in blocks if b.end_min > window_start and b.start_min < window_end]
    if not covering:
        raise PlanError(
            f"Nothing planned inside the enforced window {fmt(window_start)}-{fmt(window_end)}."
        )

    cursor = window_start
    gaps: list[str] = []
    for b in covering:
        if b.start_min > cursor:
            gaps.append(f"{fmt(cursor)}-{fmt(b.start_min)}")
        cursor = max(cursor, b.end_min)
    if cursor < window_end:
        gaps.append(f"{fmt(cursor)}-{fmt(window_end)}")

    if gaps:
        raise PlanError(
            "These stretches inside the enforced window have no block:\n" +
            "\n".join(f"  • {g}" for g in gaps) +
            "\n\nEvery minute needs a name. Use `relax` if that's the honest answer."
        )


def render(blocks: list[Block]) -> str:
    """The plan the way a person would write it back to you.

    Every message that carries this is sent as HTML, so the old markdown
    backticks arrived as literal ` characters, and an unescaped label with a
    < in it could break the whole message.
    """
    import html
    return "\n".join(f"{b.hhmm()}  {html.escape(b.label)}" for b in blocks)


def block_for(blocks: list[Block], minute: int):
    for b in blocks:
        if b.start_min <= minute < b.end_min:
            return b
    return None
