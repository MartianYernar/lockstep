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
        "blocker_blocklist_visible": {"type": "boolean"},
        "blocker_detail": {"type": "string"},
        "face_visible": {"type": "boolean"},
        "identity_visible": {"type": "boolean"},
        "location_revealing": {"type": "boolean"},
        "privacy_detail": {"type": "string"},
    },
    "required": [
        "timestamp_visible", "timestamp_text", "activity_seen",
        "activity_match", "confidence", "reason", "looks_like_screen_of_photo",
        "blocker_visible", "blocker_active", "blocker_allowlist_visible",
        "blocker_blocklist_visible", "blocker_detail",
        "face_visible", "identity_visible", "location_revealing", "privacy_detail",
    ],
}

PROMPT = """You are the verifier for a personal accountability system. Judge ONE photo.

The person committed in advance to be doing this right now:
  TASK: "{label}"
  CATEGORY: {category}
Server time right now is {now} ({tz}).

WHAT THIS PARTICULAR PHOTO IS SUPPOSED TO SHOW:
{requirement}

Report these fields, and nothing else:

timestamp_visible - true only if a date and/or clock time is BURNED INTO the
  image: a camera timestamp overlay, or a visible OS menu bar / taskbar clock
  in a screenshot. A wall clock or wristwatch in frame counts. Metadata you
  cannot see does not. If you see no readable time, false.
timestamp_text - exactly what that visible time reads, verbatim. "" if none.
activity_seen - one plain sentence: what is actually happening in the frame.
activity_match - true if the frame satisfies the requirement above.
  Be reasonable, not gullible. Judge consistency, not proof of effort.
  A task naming several things -- "stretch and mouth rinse", "shower, skincare"
  -- is a sequence carried out across the whole block, not a single instant. One
  photo can only ever show one of them, so a frame consistent with ANY named
  part is a match. Requiring all of them makes such a block impossible to pass.
  Judge the posture or the setup, not visible exertion: a still photo cannot
  show motion. Someone lying on the floor with limbs extended is consistent
  with stretching; someone at a sink is consistent with brushing or rinsing.
  Typos in the task are the person's own shorthand, not a reason to refuse --
  read through them to the obvious intent.
confidence - 0.0 to 1.0, how sure you are about activity_match.
reason - one short sentence a person would find fair, addressed to them
  directly. If activity_match is false, say concretely what was missing.
looks_like_screen_of_photo - true if this is a picture OF a screen showing
  another photo, or an obvious re-photograph of an older image.

blocker_visible - true if a website/app blocker window or overlay is on screen.
blocker_active - true if that blocker clearly shows a block currently RUNNING:
  an active session, a countdown, a "locked"/"blocked until" state. A blocker
  merely open, or showing a finished or not-yet-started block, is false.
blocker_allowlist_visible - true if a list of ALLOWED / permitted / exception
  sites is readable in the frame.
blocker_blocklist_visible - true if the frame makes the SCOPE of the block
  readable. Two shapes count, and the second is the stricter one:
    - an explicit list of BLOCKED / banned sites, or
    - an ALLOW-ONLY block: everything blocked except a named set of exceptions.
      In that mode the blocked list is legitimately EMPTY, and an empty
      "Websites" tab beside a populated "Website Exceptions" tab is proof of a
      total block, not of a missing one. Wording like "Blocked at all times",
      "No breaks" or "All users" alongside an exceptions list is the same thing.
  Only say false if the frame shows neither -- you cannot tell what the block
  actually covers.
blocker_detail - one sentence on what the blocker shows. "" if none visible.

Now three privacy fields. The person has asked to be warned about these, so
judge them strictly -- a false negative here costs them more than a retake:

face_visible - true if any human face is identifiable in the frame, theirs or
  anyone else's. A body with the face out of frame or turned away is false.
identity_visible - true if any name, username, login, email address, account
  name or profile photo is readable anywhere, including in browser tabs,
  window titles, sidebars and notification banners.
location_revealing - true if the background could place them: a window with a
  view out, buildings, a street, a recognisable landmark, house numbers,
  signage. An interior wall, floor, desk or ceiling is false.
privacy_detail - one sentence naming what would need to move out of frame.
  "" if nothing.
"""


REQUIREMENTS = {
    "blocker_toggle": (
        "A screenshot of {blocker} with a block ACTIVELY RUNNING -- the toggle on,\n"
        "  a timer counting, and the computer's clock visible in the taskbar."),
    "blocker_allow": (
        "A screenshot of {blocker} showing what this block COVERS, readable, with\n"
        "  the clock visible. Any of these settles it: the allowed / permitted\n"
        "  list, the exceptions list of an allow-only block, or an explicit list\n"
        "  of blocked sites. One shot, whichever way the block is configured."),
    "blocker_block": (
        "A screenshot of {blocker} showing what this block actually covers, with\n"
        "  the clock visible. Either a readable list of BLOCKED websites, or --\n"
        "  for an allow-only block -- an empty blocked list beside its exceptions,\n"
        "  which means everything is blocked except those. The second is the\n"
        "  stronger setup and counts."),
    "activity": (
        "A photograph of the person actually doing the task, with a visible\n"
        "  timestamp. For sport: the body mid-exercise or the equipment in use.\n"
        "  For sleep: the bed, ready. For cleaning: the thing being cleaned."),
}


@dataclass
class Verdict:
    ok: bool
    reason: str
    blocker_visible: bool = False
    blocker_active: bool = False
    blocker_allowlist_visible: bool = False
    blocker_blocklist_visible: bool = False
    blocker_detail: str = ""
    face_visible: bool = False
    identity_visible: bool = False
    location_revealing: bool = False
    privacy_detail: str = ""
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
        self.last_model = ""

    def _get_client(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.cfg.raw["gemini"]["api_key"])
        return self._client

    def _model_chain(self) -> list[str]:
        g = self.cfg.raw["gemini"]
        chain = list(g.get("models") or [])
        if g.get("model") and g["model"] not in chain:
            chain.insert(0, g["model"])
        return chain or ["gemini-3.1-flash-lite"]

    async def _ask(self, image_bytes: bytes, mime: str, prompt: str) -> dict:
        from google.genai import types
        client = self._get_client()

        # Thinking tokens are billed at the output rate and dominate the cost of
        # a job this small. Measured: with thinking on, ~890 output tokens per
        # check; with it off, ~160 -- and the verdicts came out identical, because
        # "is there a clock and does this look like a gym" is perception, not
        # reasoning. Set thinking_budget to null in config to re-enable it.
        extra = {}
        budget = self.cfg.raw["gemini"].get("thinking_budget", 0)
        if budget is not None:
            extra["thinking_config"] = types.ThinkingConfig(thinking_budget=int(budget))

        # The free tier caps requests per day *per model*, so a second model is
        # a second allowance rather than a way around one limit. It doubles as
        # plain robustness: if one model is having a bad day, the check still
        # happens instead of silently passing everything through unverified.
        cfg = types.GenerateContentConfig(
            temperature=0.0,
            response_mime_type="application/json",
            response_schema=RESPONSE_SCHEMA,
            **extra,
        )
        last: Exception | None = None
        for model in self._model_chain():
            try:
                resp = await client.aio.models.generate_content(
                    model=model,
                    contents=[types.Part.from_bytes(data=image_bytes, mime_type=mime),
                              prompt],
                    config=cfg,
                )
                if model != self._model_chain()[0]:
                    log.info("fell back to %s", model)
                self.last_model = model
                return json.loads(resp.text)
            except Exception as exc:                          # noqa: BLE001
                last = exc
                text = str(exc)
                if "RESOURCE_EXHAUSTED" in text or "429" in text:
                    log.warning("%s out of quota, trying the next model", model)
                    continue
                raise
        raise last if last else RuntimeError("no models configured")

    async def check(self, image_bytes: bytes, label: str, category: str,
                    now: datetime, mime: str = "image/jpeg",
                    kind: str = "activity") -> Verdict:
        v = self.cfg.vision
        blocker = v.get("blocker_name", "Cold Turkey")
        requirement = REQUIREMENTS.get(kind, REQUIREMENTS["activity"]).format(blocker=blocker)
        prompt = PROMPT.format(
            label=label, category=category, requirement=requirement,
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
                text = str(exc)
                if "RESOURCE_EXHAUSTED" in text or "429" in text:
                    log.error("every model is out of quota/credit; not retrying")
                    break
                log.warning("gemini attempt %d failed: %s", attempt + 1, exc)
                await asyncio.sleep(2 ** attempt)

        if data is None:
            self.consecutive_failures += 1
            quota = "RESOURCE_EXHAUSTED" in str(last_err) or "429" in str(last_err)
            why = ("Out of API quota or credit — nothing is being checked right now."
                   if quota else f"Could not reach the vision model ({last_err}).")
            return Verdict(
                ok=True, unverified=True, failure_code="unverified",
                reason=f"{why} Accepted this one, but it is flagged in the log "
                       "and your father has been told.",
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
            blocker_blocklist_visible=bool(data.get("blocker_blocklist_visible")),
            blocker_detail=data.get("blocker_detail", ""),
            face_visible=bool(data.get("face_visible")),
            identity_visible=bool(data.get("identity_visible")),
            location_revealing=bool(data.get("location_revealing")),
            privacy_detail=data.get("privacy_detail", ""),
            timestamp_visible=bool(data.get("timestamp_visible")),
            timestamp_text=stamp_text,
            timestamp_fresh=fresh,
            activity_seen=data.get("activity_seen", ""),
            activity_match=match,
            confidence=conf,
            replay_suspected=bool(data.get("looks_like_screen_of_photo")),
        )

        # --- privacy first: never send a second copy of a bad frame anywhere ---
        priv = self.cfg.raw.get("privacy", {})
        if priv.get("enforce", True):
            leaks = []
            if base["face_visible"] and priv.get("no_faces", True):
                leaks.append("a face")
            if base["identity_visible"] and priv.get("no_identity", True):
                leaks.append("your name or login")
            if base["location_revealing"] and priv.get("no_location", True):
                leaks.append("something that shows where you are")
            if leaks:
                return Verdict(
                    ok=False, failure_code="privacy",
                    reason=f"Not counted — this shows {', '.join(leaks)}. "
                           f"{base['privacy_detail']} Reframe and send it again. "
                           "(Your rule, not mine.)", **base)

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

        # --- what this specific shot was asked for ---
        if kind.startswith("blocker"):
            if not base["blocker_visible"]:
                return Verdict(ok=False, failure_code="no_blocker",
                               reason=f"No {blocker} window in the shot. Full-screen "
                                      f"screenshot showing the blocker, please.", **base)
            if kind == "blocker_toggle" and not base["blocker_active"]:
                return Verdict(ok=False, failure_code="blocker_off",
                               reason=f"{blocker} is on screen but the block isn't running. "
                                      f"Start it, then screenshot it.", **base)
            # Either list settles it. The point of this shot is "what does the
            # block cover", and an allow-only block answers that with its
            # exceptions while a deny-list answers it with the blocked names.
            # Demanding one specific tab made a correctly configured block
            # unpassable.
            if kind == "blocker_allow" and not (base["blocker_allowlist_visible"]
                                                or base["blocker_blocklist_visible"]):
                return Verdict(ok=False, failure_code="no_allowlist",
                               reason="I can't see what this block covers. Open the block's "
                                      "details so its list of sites shows.", **base)
            if kind == "blocker_block" and not base["blocker_blocklist_visible"]:
                return Verdict(ok=False, failure_code="no_blocklist",
                               reason="I can't read the blocked-sites list. Open the block's "
                                      "details so the list shows.", **base)
            return Verdict(ok=True, failure_code="ok",
                           reason=data.get("reason") or "Counted.", **base)

        if not match:
            return Verdict(ok=False, failure_code="mismatch",
                           reason=data.get("reason") or
                                  f"That doesn't look like '{label}'.", **base)
        return Verdict(ok=True, failure_code="ok",
                       reason=data.get("reason") or "Counted.", **base)
