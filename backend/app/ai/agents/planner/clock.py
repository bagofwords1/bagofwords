"""Shared 'current time' rendering for planner prompts.

Renders the per-turn timestamp in the organization's configured timezone when
one is set (Settings → General), falling back to the server's local timezone —
the prior behaviour — when it is unset or invalid. Storage is unaffected; this
only changes the clock the model is told about.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def now_in_org_tz(timezone: Optional[str]) -> Tuple[datetime, str]:
    """Return (now, tz_label) for the org timezone, or local time as fallback."""
    if timezone:
        try:
            now = datetime.now(ZoneInfo(timezone))
            return now, timezone
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            pass
    now = datetime.now().astimezone()
    return now, str(now.tzinfo)


def current_time_str(timezone: Optional[str]) -> str:
    """'YYYY-MM-DD HH:MM:SS; timezone: <tz>' in the org tz (local fallback)."""
    now, tz = now_in_org_tz(timezone)
    return f"{now.strftime('%Y-%m-%d %H:%M:%S')}; timezone: {tz}"


def time_block(timezone: Optional[str]) -> str:
    """'<time>YYYY-MM-DD HH:MM:SS (<tz>)</time>' in the org tz (local fallback)."""
    now, tz = now_in_org_tz(timezone)
    return f"<time>{now.strftime('%Y-%m-%d %H:%M:%S')} ({tz})</time>"
