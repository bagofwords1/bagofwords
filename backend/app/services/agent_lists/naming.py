"""Slugs, tool names and table names for lists.

Names are for discovery only — execution always resolves by id, so renaming an
agent or a list never breaks a saved query (see bow_source ``list_id``).
"""
import hashlib
import re
import unicodedata
from typing import Iterable, Optional

TOOL_PREFIX = "submit_"
MAX_TOOL_NAME = 64


def slugify(value: str, *, fallback_prefix: str, seed: str, max_len: int = 40) -> str:
    """ASCII snake_case slug; non-Latin names fall back to ``<prefix>_<hash>``."""
    norm = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "_", norm.lower()).strip("_")
    if not slug or not re.match(r"[a-z]", slug):
        slug = f"{fallback_prefix}_{hashlib.sha1((seed or value or '').encode()).hexdigest()[:8]}"
    return slug[:max_len].rstrip("_")


def unique_slug(base: str, taken: Iterable[str]) -> str:
    taken = set(taken)
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


def agent_slug(name: str, agent_id: str) -> str:
    return slugify(name, fallback_prefix="agent", seed=str(agent_id), max_len=32)


def tool_name_for(list_slug: str, *, agent_slug_value: Optional[str] = None, list_id: str = "") -> str:
    """``submit_<slug>`` (optionally agent-qualified), capped at 64 chars."""
    name = f"{TOOL_PREFIX}{list_slug}"
    if agent_slug_value:
        name = f"{name}_{agent_slug_value}"
    if len(name) > MAX_TOOL_NAME:
        suffix = "_" + hashlib.sha1((list_id or name).encode()).hexdigest()[:6]
        name = name[: MAX_TOOL_NAME - len(suffix)].rstrip("_") + suffix
    return name


def table_name_for(agent_name: str, agent_id: str, list_slug: str) -> str:
    return f"bow.{agent_slug(agent_name, agent_id)}.lists.{list_slug}"
