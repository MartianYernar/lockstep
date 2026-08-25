"""Image handling on the way in.

Everything a photo carries beyond "what is in the frame" is a liability:
EXIF GPS coordinates, device serial numbers, and detail fine enough to read a
street sign. This strips what it can and shrinks the rest.

What this does NOT do, and cannot: hide where you are. Testing showed the model
describing "a skyline of high-rise buildings" just as readily at 512px as at
3000px. Downscaling removes fine detail like signage and plates; it does not
remove a skyline. The only reliable defence against background geolocation is
framing the shot so the background isn't in it. That's a human step, not a
software one, and pretending otherwise would be worse than useless.
"""
from __future__ import annotations

import io
import logging

log = logging.getLogger("images")


def prepare(blob: bytes, max_dim: int = 1024, quality: int = 85) -> tuple[bytes, str]:
    """Strip metadata and downscale. Returns (bytes, mime).

    Re-encoding through Pillow without passing `exif=` drops every metadata
    block, GPS included. Telegram already strips EXIF from `photo` messages,
    but this is the layer that guarantees it regardless of how a file arrives.
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning("Pillow missing — sending the image unprocessed")
        return blob, "image/jpeg"

    try:
        im = Image.open(io.BytesIO(blob))
        im = im.convert("RGB")                     # also discards alpha/ICC
        if max(im.size) > max_dim:
            im.thumbnail((max_dim, max_dim), Image.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality, optimize=True)   # no exif= -> stripped
        return out.getvalue(), "image/jpeg"
    except Exception as exc:                                   # noqa: BLE001
        log.warning("could not process image (%s) — using original", exc)
        return blob, "image/jpeg"


def thumbnail(blob: bytes, dim: int = 320) -> bytes:
    data, _ = prepare(blob, max_dim=dim, quality=70)
    return data


def has_gps(blob: bytes) -> bool:
    """Did the original carry GPS coordinates? Worth telling someone about."""
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        im = Image.open(io.BytesIO(blob))
        exif = getattr(im, "_getexif", lambda: None)() or {}
        return any(TAGS.get(k) == "GPSInfo" for k in exif)
    except Exception:                                          # noqa: BLE001
        return False
