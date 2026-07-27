"""Small, provider-neutral helpers shared by native mail connectors."""

from __future__ import annotations

import html
import re

_BREAK_TAG_RE = re.compile(
    r"<\s*(?:br\s*/?|/p|/div|/li|/tr|/h[1-6])\s*>",
    flags=re.IGNORECASE,
)
_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: str) -> str:
    """Render a conservative plain-text representation of an HTML email.

    This is deliberately not an HTML sanitizer for browser rendering: mail
    bodies never reach the browser as HTML.  It preserves common block breaks,
    removes tags, decodes entities, and normalizes whitespace for the model.
    """
    text = _BREAK_TAG_RE.sub("\n", value or "")
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
