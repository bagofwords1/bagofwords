"""Pure helpers shared by the dreams: settings, the night window, constants.

Everything here is side-effect free so it can be unit tested without a DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------- settings keys

SETTING_AGENT_DREAMING = "enable_agent_dreaming"
SETTING_EXPIRY_DAYS = "ai_suggestion_expiry_days"
SETTING_USER_MEMORY = "enable_user_memory"
SETTING_CHECKINS = "enable_agent_checkins"

DEFAULT_EXPIRY_DAYS = 30
MAX_EXPIRY_DAYS = 3650

# ----------------------------------------------------------------- constants

# Org-local hours. User dreams first, agent dreams after.
USER_WINDOW = (1, 3)    # [01:00, 03:00)
AGENT_WINDOW = (3, 5)   # [03:00, 05:00)

# Nightly token budget per org across both dream kinds.
DREAM_ORG_NIGHTLY_TOKENS = 2_000_000
# Concurrency: global and per org.
GLOBAL_CONCURRENCY = 4
PER_ORG_CONCURRENCY = 2
# A run left 'running' longer than this is failed as stale.
STALE_RUNNING_MINUTES = 60
# Max units queued per org per kind per tick (the rest wait for the next hour).
MAX_UNITS_PER_ORG_PER_TICK = 100

# Agent dream promotion gates.
MIN_DISTINCT_USERS = 3
MIN_DISTINCT_DAYS = 2
# Expired drafts keep counting as evidence for this long.
EXPIRED_EVIDENCE_DAYS = 90
# AI instructions not used for this long are proposed for archive.
UNUSED_ARCHIVE_DAYS = 60

# User dream limits.
MAX_DREAM_CHECKINS = 2
# A memory event this close wakes the user dream even without new activity.
UPCOMING_EVENT_DAYS = 3


def _cfg_value(org_settings: Any, key: str, default: Any) -> Any:
    """Read a FeatureConfig value from an OrganizationSettings row (or None)."""
    if org_settings is None:
        return default
    try:
        cfg = org_settings.get_config(key)
    except Exception:
        cfg = None
    if cfg is None:
        return default
    value = getattr(cfg, "value", cfg)
    if isinstance(value, dict) and "value" in value:
        value = value["value"]
    return default if value is None else value


def _bool(org_settings: Any, key: str, default: bool) -> bool:
    return bool(_cfg_value(org_settings, key, default))


def agent_dreaming_enabled(org_settings: Any) -> bool:
    return _bool(org_settings, SETTING_AGENT_DREAMING, True)


def user_dreaming_enabled(org_settings: Any) -> bool:
    """The user dream has no switch of its own: it keeps user memory up to
    date (and plans check-ins, which follow their own switch and opt-out),
    so it runs whenever user memory is on."""
    return _bool(org_settings, SETTING_USER_MEMORY, True)


def checkins_enabled(org_settings: Any) -> bool:
    return _bool(org_settings, SETTING_CHECKINS, True)


def expiry_days(org_settings: Any) -> int:
    """Days before a pending AI suggestion expires. 0 = never."""
    raw = _cfg_value(org_settings, SETTING_EXPIRY_DAYS, DEFAULT_EXPIRY_DAYS)
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_EXPIRY_DAYS
    if days < 0:
        return 0
    return min(days, MAX_EXPIRY_DAYS)


def org_timezone(org_settings: Any) -> str:
    try:
        tz = (getattr(org_settings, "config", None) or {}).get("timezone")
    except Exception:
        tz = None
    if tz:
        try:
            ZoneInfo(tz)
            return tz
        except Exception:
            pass
    return "UTC"


def utcnow() -> datetime:
    """Naive UTC, like every stored timestamp."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_now(now_utc: datetime, tz_name: str) -> datetime:
    return now_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))


def local_date(now_utc: datetime, tz_name: str) -> str:
    return local_now(now_utc, tz_name).strftime("%Y-%m-%d")


def in_window(now_utc: datetime, tz_name: str, window: tuple) -> bool:
    start, end = window
    hour = local_now(now_utc, tz_name).hour
    return start <= hour < end


def window_for(kind: str) -> tuple:
    return USER_WINDOW if kind == "user" else AGENT_WINDOW


def day_key(dt: Optional[datetime]) -> Optional[str]:
    return dt.strftime("%Y-%m-%d") if dt else None


def days_ago(now: datetime, days: int) -> datetime:
    return now - timedelta(days=days)
