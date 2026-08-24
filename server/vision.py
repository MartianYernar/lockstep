"""Photo validation via Gemini.

Two independent things get checked, and both must pass:

  1. FRESHNESS -- the photo must carry a *visible, burned-in* clock (phone camera
     timestamp stamp, or the menu bar in a full-screen laptop screenshot), and
     that clock must be close to the server's current time.  This is what stops
     a roll of photos taken on Sunday from covering all of Monday.
  2. ACTIVITY  -- what's in the frame has to plausibly be the block you planned.

Failing #1 is not a judgement call, so the message back says exactly what was
missing.  Failing #2 is fuzzy, so it comes with the model's reason attached and
you can retry with a clearer shot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

log = logging.getLogger("vision")

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "timestamp_visible": {"type": "boolean"},
        "timestamp_text": {"type": "string"},
        "activity_seen": {"type": "string"},
        "activity_match": {"type": "boolean"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "looks_like_screen_of_photo": {"type": "boolean"},
        "blocker_visible": {"type": "boolean"},
        "blocker_active": {"type": "boolean"},
        "blocker_allowlist_visible": {"type": "boolean"},
        "blocker_detail": {"type": "string"},
    },
    "required": [
        "timestamp_visible", "timestamp_text", "activity_seen",
        "activity_match", "confidence", "reason", "looks_like_screen_of_photo",
        "blocker_visible", "blocker_active", "blocker_allowlist_visible", "blocker_detail",
    ],
}

PROMPT = """You are the verifier for a personal accountability system. Judge ONE photo.

The person committed in advance to be doing this right now:
  TASK: "{label}"
  CATEGORY: {category}
The photo is supposed to be proof they are doing it.
Server time right now is {now} ({tz}).

Report these fields, and nothing else:

timestamp_visible - true only if a date and/or clock time is BURNED INTO the
  image itself: a camera timestamp overlay, or a visible OS menu bar / taskbar
  clock in a screenshot. A clock on a wall or a wristwatch in frame also counts.
  Metadata you cannot see does not count. If you see no readable time, false.
timestamp_text - exactly what that visible time reads, verbatim. "" if none.
activity_seen - one plain sentence: what is actually happening in the frame.
activity_match - true if the frame is consistent with the committed TASK.
  Be reasonable, not gullible. A laptop with a linear algebra PDF open matches
  "work: linear algebra pset". A laptop showing YouTube does not. A gym floor,
  weights or a treadmill matches "gym". A dark ceiling matches nothing.
  Judge consistency, not proof of effort.
confidence - 0.0 to 1.0, how sure you are about activity_match.
reason - one short sentence a person would find fair, addressed to them
  directly. If activity_match is false, say concretely what was missing.
looks_like_screen_of_photo - true if this is a picture OF a screen showing
  another photo, or an obvious re-photograph of an older image.

blocker_visible - true if a website/app blocker window or overlay is on screen.
blocker_active - true if that blocker clearly shows a block currently RUNNING:
  an active session, a countdown, a "locked"/"blocked until" state. A blocker
  that is merely open, or showing a finished or not-yet-started block, is false.
blocker_allowlist_visible - true if the list of allowed or blocked sites is
  readable in the frame.
blocker_detail - one sentence on what the blocker shows: its state, any timer,
  and which sites are listed. "" if no blocker is visible.
{blocker_note}"""


@dataclass
class Verdict:
    ok: bool
    reason: str
    blocker_visible: bool = False
    blocker_active: bool = False
    blocker_allowlist_visible: bool = False
    blocker_detail: str = ""
    timestamp_visible: bool = False
    timestamp_text: str = ""
    timestamp_fresh: bool = False
    activity_seen: str = ""
    activity_match: bool = False
    confidence: float = 0.0
    replay_suspected: bool = False
    unverified: bool = False       # the model never answered; accepted under protest
    failure_code: str = ""         # no_timestamp | stale | mismatch | replay | ok | unverified

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)


# Accepts 16:47, 16.47, 4:47 PM, and ignores any surrounding date text.
CLOCK_RE = re.compile(r"(\d{1,2})\s*[:.]\s*(\d{2})\s*(am|pm)?", re.IGNORECASE)


def _read_clock(text: str, now: datetime) -> datetime | None:
    """Pull a wall-clock time out of the model's transcription of the stamp.

    Camera stamps bury the clock in a date ("24.08.2026 16:45"), and a dotted
    date matches the clock pattern too.  So collect every candidate, throw away
    the ones that aren't valid times, and keep whichever sits closest to now.
    """
    candidates: list[datetime] = []
    for m in CLOCK_RE.finditer(text or ""):
        h, mi, mer = int(m.group(1)), int(m.group(2)), m.group(3)
        if mer:
            mer = mer.lower()
            if mer == "pm" and h != 12:
                h += 12
            elif mer == "am" and h == 12:
                h = 0
        if not (0 <= h < 24 and 0 <= mi < 60):
            continue                      # e.g. the "24.08" of a date
        stamped = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        # A stamp reading 23:58 seen at 00:03 belongs to yesterday, and vice versa.
        best = min(
            (stamped, stamped - timedelta(days=1), stamped + timedelta(days=1)),
            key=lambda d: abs((d - now).total_seconds()),
        )
        candidates.append(best)
    if not candidates:
        return None
    return min(candidates, key=lambda d: abs((d - now).total_seconds()))


class Validator:
    def __init__(self, cfg):
        self.cfg = cfg
        self._client = None
        self.consecutive_failures = 0

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.cfg.raw["gemini"]["api_key"])
        return self._client

    async def _ask(self, image_bytes: bytes, mime: str, prompt: str) -> dict:
        from google.genai import types
        client = self._get_client()
        resp = await client.aio.models.generate_content(
            model=self.cfg.raw["gemini"]["model"],
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime),
                prompt,
            ],
            config=types.GenerateContentConfig(
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=RESPONSE_SCHEMA,
            ),
        )
        return json.loads(resp.text)

    async def check(self, image_bytes: bytes, label: str, category: str,
                    now: datetime, mime: str = "image/jpeg",
                    require_blocker: bool = False) -> Verdict:
        v = self.cfg.vision
        blocker = v.get("blocker_name", "Cold Turkey")
        note = ""
        if require_blocker:
            note = (f"\nTHIS ONE IS A BLOCKER CHECK. The image should be a full-screen "
                    f"screenshot of a computer showing {blocker} with a block actively "
                    f"running, its allow/block list readable, and the OS taskbar clock "
                    f"visible. Judge the blocker fields with care -- they decide the result.")
        prompt = PROMPT.format(
            label=label, category=category, blocker_note=note,
            now=now.strftime("%Y-%m-%d %H:%M"), tz=str(self.cfg.tz),
        )

        data: dict | None = None
        last_err: Exception | None = None
        for attempt in range(int(v.get("max_retries", 3))):
            try:
                data = await self._ask(image_bytes, mime, prompt)
                break
            except Exception as exc:                        # noqa: BLE001
                last_err = exc
                log.warning("gemini attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)

        if data is None:
            # Infrastructure failed, not the person. Accept, but mark it loudly:
            # a silently-accepted photo would otherwise be farmable during an outage.
            self.consecutive_failures += 1
            return Verdict(
                ok=True, unverified=True, failure_code="unverified",
                reason=f"Could not reach the vision model ({last_err}). "
                       "Accepted this one, but it is flagged in the log.",
            )
        self.consecutive_failures = 0

        stamp_text = (data.get("timestamp_text") or "").strip()
        stamped = _read_clock(stamp_text, now) if data.get("timestamp_visible") else None
        tolerance = int(v.get("timestamp_tolerance_min", 12))
        fresh = stamped is not None and abs((stamped - now).total_seconds()) <= tolerance * 60
        drift_min = int(abs((stamped - now).total_seconds()) // 60) if stamped else 0

        conf = float(data.get("confidence", 0.0))
        match = bool(data.get("activity_match")) and conf >= float(v.get("min_confidence", 0.55))

        base = dict(
            blocker_visible=bool(data.get("blocker_visible")),
            blocker_active=bool(data.get("blocker_active")),
            blocker_allowlist_visible=bool(data.get("blocker_allowlist_visible")),
            blocker_detail=data.get("blocker_detail", ""),
            timestamp_visible=bool(data.get("timestamp_visible")),
            timestamp_text=stamp_text,
            timestamp_fresh=fresh,
            activity_seen=data.get("activity_seen", ""),
            activity_match=match,
            confidence=conf,
            replay_suspected=bool(data.get("looks_like_screen_of_photo")),
        )

        if base["replay_suspected"]:
            return Verdict(ok=False, failure_code="replay",
                           reason="That looks like a photo of a screen showing another photo. "
                                  "Take a fresh one.", **base)
        if not base["timestamp_visible"]:
            return Verdict(ok=False, failure_code="no_timestamp",
                           reason="No visible clock in the frame. Turn the camera's timestamp "
                                  "stamp back on, or include a screen showing the time.", **base)
        if not fresh:
            return Verdict(ok=False, failure_code="stale",
                           reason=f"The stamp reads '{stamp_text}' — about {drift_min} min off "
                                  f"from now. Needs to be within {tolerance} min.", **base)
        if require_blocker:
            if not base["blocker_visible"]:
                return Verdict(ok=False, failure_code="no_blocker",
                               reason=f"No {blocker} window in the shot. This check needs a "
                                      f"full-screen screenshot showing the blocker running.",
                               **base)
            if not base["blocker_active"]:
                return Verdict(ok=False, failure_code="blocker_off",
                               reason=f"{blocker} is on screen but the block isn't running. "
                                      f"Start the block, then screenshot it.", **base)
            if not base["blocker_allowlist_visible"]:
                return Verdict(ok=False, failure_code="no_allowlist",
                               reason="The allowed/blocked site list isn't readable. Open the "
                                      "block's details so the list shows.", **base)

        if not match:
            return Verdict(ok=False, failure_code="mismatch",
                           reason=data.get("reason") or
                                  f"That doesn't look like '{label}'.", **base)
        return Verdict(ok=True, failure_code="ok",
                       reason=data.get("reason") or "Counted.", **base)
