"""His day, as he actually writes it: a Google Doc with a list per day.

10 Oct 2026. The task list in `config.yaml` had been seeded in September and
never moved on, so a fresh database brought back "Physics IA, due 20 Sep" --
a thing he had finished and said so -- and the bot told him it was twenty days
overdue. He keeps the real list somewhere else, by hand, every day:

    **MONDAY 5 oct**
    **School hours:**
    - [ ] finish rewriting reflection of may
    - [x] ~~finish CAS summer~~

So read it there. A Google Doc published with a link exports as Markdown at
/export?format=md, and the Markdown keeps the checkbox state -- which the
plain-text export throws away. That one detail is the whole reason this works
without any Google credentials, OAuth or API key.

What this is NOT: the enforcement record. Ticking a box in the doc proves
nothing and satisfies no checkpoint -- photos still do that, and only photos.
This is what he intends to do, which is exactly what the planner was missing.
"""
from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta

log = logging.getLogger("dailydoc")

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# "**MONDAY 5 oct**", "TUESDAY 13 October", "SUNDAY 4 oct   tonight ..."
DAY_RE = re.compile(
    r"^\s*[*#\s]*(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)[a-z]*[,\s]+"
    r"(\d{1,2})\s*(?:st|nd|rd|th)?\s+([a-z]{3,9})\b", re.I)
# the same, written the other way round: "oct 13", "October 13"
DAY_RE_ALT = re.compile(
    r"^\s*[*#\s]*(?:mon|tues?|wed(?:nes)?|thur?s?|fri|sat(?:ur)?|sun)[a-z]*[,\s]+"
    r"([a-z]{3,9})\s+(\d{1,2})\b", re.I)
ITEM_RE = re.compile(r"^\s*[-*+]\s*\[([ xX])\]\s*(.*)$")
# a bold line that is not a day and not an item -- "**School hours:**",
# "**60 min   EE: the plan only**". Worth keeping: it says what the group is.
GROUP_RE = re.compile(r"^\s*\*\*(.+?)\*\*\s*:?\s*$")


def export_url(doc: str) -> str:
    """Accepts a doc id, an /edit link or an export link; returns the md export."""
    doc = (doc or "").strip()
    m = re.search(r"/document/d/([A-Za-z0-9_-]{20,})", doc)
    doc_id = m.group(1) if m else doc
    return f"https://docs.google.com/document/d/{doc_id}/export?format=md"


def _clean(text: str) -> str:
    """Strip the markdown a ticked box arrives wrapped in."""
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"\\([.\-*_])", r"\1", text)       # \. from the exporter
    return re.sub(r"\s+", " ", text).strip()


def _as_date(day: int, month_word: str, today: date) -> date | None:
    month = MONTHS.get(month_word[:3].lower())
    if not month or not 1 <= day <= 31:
        return None
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            d = date(year, month, day)
        except ValueError:
            continue
        # The nearest one in either direction: a doc written in December that
        # says "3 jan" means next year, not eleven months ago.
        if abs((d - today).days) <= 183:
            return d
    return None


def parse(md: str, today: date) -> list[dict]:
    """Markdown in, one entry per dated section out, in document order."""
    out: list[dict] = []
    cur: dict | None = None
    group = ""
    for line in (md or "").splitlines():
        m = DAY_RE.match(line) or DAY_RE_ALT.match(line)
        if m:
            a, b = m.group(1), m.group(2)
            d = (_as_date(int(a), b, today) if a.isdigit()
                 else _as_date(int(b), a, today))
            if d is not None:
                cur = {"date": d, "heading": _clean(line), "items": []}
                out.append(cur)
                group = ""
                continue
        item = ITEM_RE.match(line)
        if item and cur is not None:
            text = _clean(item.group(2))
            if text:
                cur["items"].append({"text": text,
                                     "done": item.group(1).lower() == "x",
                                     "group": group})
            continue
        g = GROUP_RE.match(line)
        if g and not item:
            group = _clean(g.group(1))
    return out


class DailyDoc:
    """Fetches the doc, remembers the last good copy, answers for one day."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._text: str | None = None
        self._at: datetime | None = None

    @property
    def conf(self) -> dict:
        return self.cfg.raw.get("daily_doc", {}) or {}

    @property
    def enabled(self) -> bool:
        return bool(self.conf.get("enabled")) and bool(self.conf.get("url"))

    @property
    def url(self) -> str:
        return export_url(str(self.conf.get("url") or ""))

    def _fresh(self, now: datetime) -> bool:
        ttl = int(self.conf.get("refresh_min", 20))
        return self._at is not None and (now - self._at) < timedelta(minutes=ttl)

    async def text(self, now: datetime, force: bool = False) -> str | None:
        """The doc, from cache unless it is stale. None if it has never loaded.

        A doc that cannot be fetched must never stop a day being planned, so
        every failure returns the last good copy and says so in the log.
        """
        if not self.enabled:
            return None
        if self._text is not None and self._fresh(now) and not force:
            return self._text
        import aiohttp
        try:
            timeout = aiohttp.ClientTimeout(total=int(self.conf.get("timeout_sec", 15)))
            async with aiohttp.ClientSession(timeout=timeout) as s:
                async with s.get(self.url) as r:
                    if r.status != 200:
                        log.warning("doc fetch: HTTP %s", r.status)
                        return self._text
                    body = await r.text()
        except Exception as exc:                              # noqa: BLE001
            log.warning("doc fetch failed: %s", exc)
            return self._text
        if "<html" in body[:200].lower():
            # Google hands back a sign-in page when the link is not shared.
            log.warning("doc fetch: got HTML, the doc is probably not link-shared")
            return self._text
        self._text, self._at = body, now
        return body

    async def sections(self, now: datetime, force: bool = False) -> list[dict]:
        md = await self.text(now, force=force)
        return parse(md or "", now.date())

    async def for_day(self, day: date, now: datetime, force: bool = False) -> dict | None:
        for sec in await self.sections(now, force=force):
            if sec["date"] == day:
                return sec
        return None

    @staticmethod
    def describe(section: dict | None, include_done: bool = True) -> str:
        """The day's list, written for a model or for him."""
        if not section or not section["items"]:
            return ""
        lines, group = [], None
        for it in section["items"]:
            if not include_done and it["done"]:
                continue
            if it["group"] and it["group"] != group:
                group = it["group"]
                lines.append(f"  [{group}]")
            lines.append(f"  {'[x]' if it['done'] else '[ ]'} {it['text']}")
        return "\n".join(lines)

    @staticmethod
    def open_items(section: dict | None) -> list[dict]:
        return [i for i in (section or {}).get("items", []) if not i["done"]]
