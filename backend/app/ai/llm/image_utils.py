"""Vision image normalization shared by the tools layer and the LLM clients.

Providers enforce hard per-image byte caps — Bedrock Converse rejects the whole
request with a 400 ValidationException past ~3.75 MB raw (5 MB base64), and a
single oversized page render used to kill the planner turn outright. Anthropic
also downscales anything past ~1568 px on the long edge server-side, so bytes
beyond that resolution buy latency and tokens, never fidelity.

Policy (mirrors Claude Code's own harness): cap the long edge at 1568 px, keep
PNG for content that compresses well (text pages, diagrams), and re-encode as
JPEG once a PNG passes 500 KB — scanned documents and photos are exactly the
content PNG is the wrong codec for. Everything is best-effort: on any decode or
encode failure the original bytes pass through unchanged.
"""

import base64
import io
import logging
from typing import Optional, Tuple

from app.ai.llm.types import ImageInput

logger = logging.getLogger(__name__)

# Anthropic standard-tier optimum: larger long edges are downscaled server-side.
VISION_MAX_EDGE_PX = 1568
# Past this a PNG is re-encoded as JPEG (Claude Code uses the same threshold).
VISION_PNG_BYTE_THRESHOLD = 500 * 1024
# Per-image ceiling for anything sent to a provider — comfortably under
# Bedrock Converse's ~3.75 MB raw / 5 MB base64 per-image limit (the tightest
# cap across the providers we support).
PROVIDER_SAFE_IMAGE_BYTES = int(3.5 * 1024 * 1024)

# Formats every vision provider accepts as-is; anything else must be re-encoded.
_PASSTHROUGH_FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg",
                        "GIF": "image/gif", "WEBP": "image/webp"}


def sniff_image_mime(data: bytes, default: str = "image/png") -> str:
    """Mime type from magic bytes, so bare byte strings (the file cache stores
    them without metadata) stay self-describing after JPEG re-encoding."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:4] in (b"GIF8",):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return default


def encode_pil_for_vision(im) -> Tuple[bytes, str]:
    """Encode a PIL image for a vision model: downscale to the long-edge cap,
    prefer PNG, fall back to JPEG when the PNG is heavyweight. Returns
    ``(bytes, mime)``."""
    from PIL import Image

    if max(im.size) > VISION_MAX_EDGE_PX:
        scale = VISION_MAX_EDGE_PX / max(im.size)
        im = im.resize((max(1, round(im.width * scale)),
                        max(1, round(im.height * scale))),
                       Image.LANCZOS)
    if im.mode not in ("RGB", "RGBA", "L", "LA"):
        im = im.convert("RGB")

    buf = io.BytesIO()
    im.save(buf, format="PNG")
    png = buf.getvalue()
    if len(png) <= VISION_PNG_BYTE_THRESHOLD:
        return png, "image/png"

    # Heavy PNG → photographic/scanned content; JPEG it. Alpha must be
    # flattened (JPEG has none) — composite onto white, matching how the page
    # would print.
    if im.mode in ("RGBA", "LA"):
        background = Image.new("RGB", im.size, (255, 255, 255))
        background.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
        im = background
    elif im.mode != "RGB" and im.mode != "L":
        im = im.convert("RGB")
    jpeg = None
    for quality in (85, 65):
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality, optimize=True)
        jpeg = buf.getvalue()
        if len(jpeg) <= VISION_PNG_BYTE_THRESHOLD * 2:
            break
    # Flat-graphics edge case where JPEG somehow beats nothing: keep whichever
    # encoding is actually smaller.
    return (jpeg, "image/jpeg") if len(jpeg) < len(png) else (png, "image/png")


def normalize_image_bytes(data: bytes, media_type: Optional[str] = None) -> Tuple[bytes, str]:
    """Normalize raw image bytes for vision use. Small images in a format every
    provider accepts pass through untouched; oversized or exotic-format ones are
    downscaled/re-encoded. Best-effort: undecodable bytes pass through."""
    mime = sniff_image_mime(data, default=media_type or "image/png")
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data))
        fmt = im.format
        # PNG passes through only while light; formats that are already lossy
        # (JPEG/WebP/GIF) are kept up to the provider ceiling — re-encoding
        # them only degrades quality.
        byte_cap = (VISION_PNG_BYTE_THRESHOLD if fmt == "PNG"
                    else PROVIDER_SAFE_IMAGE_BYTES)
        if (fmt in _PASSTHROUGH_FORMATS
                and max(im.size) <= VISION_MAX_EDGE_PX
                and len(data) <= byte_cap):
            return data, _PASSTHROUGH_FORMATS[fmt]
        im.load()
        return encode_pil_for_vision(im)
    except Exception as e:
        logger.info("normalize_image_bytes: passing through un-normalized image: %s", e)
        return data, mime


def normalize_image_input(img: ImageInput) -> ImageInput:
    """Safety net at the LLM-client boundary: shrink a base64 ImageInput that
    would breach a provider's per-image limit. Anything already within
    ``PROVIDER_SAFE_IMAGE_BYTES`` is returned as-is (no quality loss for
    normal-sized images); URL sources are never touched."""
    if img.source_type != "base64":
        return img
    try:
        raw = base64.b64decode(img.data)
    except Exception:
        return img
    if len(raw) <= PROVIDER_SAFE_IMAGE_BYTES:
        return img
    data, mime = normalize_image_bytes(raw, img.media_type)
    if len(data) >= len(raw):
        return img
    logger.info(
        "normalize_image_input: image reduced %d -> %d bytes (%s) to fit provider limit",
        len(raw), len(data), mime,
    )
    return ImageInput(data=base64.b64encode(data).decode("utf-8"),
                      media_type=mime, source_type="base64")
