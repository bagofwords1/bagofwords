"""Reindex schedule resolution — interval vs. fixed time-of-day.

Shared by the background sweeper (`scheduled_reindex.sweep_due_reindexes`) and
its tests so the "is this connection due?" / "when does it next run?" logic lives
in exactly one place.

A connection's schedule is EITHER:

  * ``interval`` — fire every ``effective_reindex_interval_minutes`` (1m floor).
  * ``time``     — fire once per day at ``reindex_at_time`` ("HH:MM"), interpreted
                   in the org's configured timezone (UTC if unset / invalid).

Storage stays UTC everywhere (``last_synced_at`` / ``next_retry_at``). The org
timezone only governs how a wall-clock "HH:MM" is mapped onto the UTC timeline —
we never persist local time.
"""
from __future__ import annotations

import logging
from datetime import datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

UTC = ZoneInfo("UTC")


def parse_hhmm(value: str | None) -> dt_time | None:
    """Parse a "HH:MM" 24h string into a ``time``; None if absent/malformed."""
    if not value:
        return None
    try:
        hh, mm = value.strip().split(":")
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return dt_time(hour=h, minute=m)
    except (ValueError, AttributeError):
        pass
    return None


def resolve_timezone(name: str | None) -> ZoneInfo:
    """Resolve an IANA tz name to a ZoneInfo, falling back to UTC."""
    if not name:
        return UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        logger.warning("reindex_schedule.bad_timezone", extra={"tz": name})
        return UTC


async def get_org_timezone(db, organization_id: str) -> ZoneInfo:
    """The org's configured display/schedule timezone (UTC fallback).

    Reads the ``timezone`` key off OrganizationSettings.config — the same JSON
    bag that carries ``locale``. Any miss/error resolves to UTC so the sweeper
    is never blocked by a settings problem.
    """
    try:
        from sqlalchemy import select
        from app.models.organization_settings import OrganizationSettings

        row = (
            await db.execute(
                select(OrganizationSettings).where(
                    OrganizationSettings.organization_id == str(organization_id)
                )
            )
        ).scalars().first()
        if row and isinstance(row.config, dict):
            return resolve_timezone(row.config.get("timezone"))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("reindex_schedule.org_tz_failed", extra={"error": str(exc)})
    return UTC


def _as_utc(dt: datetime) -> datetime:
    """Treat a stored (naive == UTC) datetime as tz-aware UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _last_time_occurrence(now_utc: datetime, at: dt_time, tz: ZoneInfo) -> datetime:
    """Most recent daily-at-`at` occurrence at-or-before `now`, returned in UTC."""
    now_local = now_utc.astimezone(tz)
    today_local = now_local.replace(
        hour=at.hour, minute=at.minute, second=0, microsecond=0
    )
    if today_local > now_local:
        today_local -= timedelta(days=1)
    return today_local.astimezone(UTC)


def is_due(connection, now_utc: datetime, tz: ZoneInfo) -> bool:
    """True if this connection's catalog is stale past its schedule.

    A never-indexed connection (NULL ``last_synced_at``) is always due.
    """
    now_utc = _as_utc(now_utc)
    last = connection.last_synced_at

    if (connection.reindex_schedule_mode or "interval") == "time":
        at = parse_hhmm(connection.reindex_at_time)
        if at is None:
            # Misconfigured time mode — fall back to interval cadence so the
            # connection still refreshes rather than silently never running.
            return _interval_due(connection, now_utc, last)
        occurrence = _last_time_occurrence(now_utc, at, tz)
        if last is None:
            return True
        return _as_utc(last) < occurrence

    return _interval_due(connection, now_utc, last)


def _interval_due(connection, now_utc: datetime, last) -> bool:
    if last is None:
        return True
    interval = timedelta(minutes=connection.effective_reindex_interval_minutes)
    return (now_utc - _as_utc(last)) >= interval


def next_run_after(connection, now_utc: datetime, tz: ZoneInfo) -> datetime:
    """When this connection should next be eligible — used as the backoff gate
    (`next_retry_at`). Returned in (naive) UTC to match the column."""
    now_utc = _as_utc(now_utc)

    if (connection.reindex_schedule_mode or "interval") == "time":
        at = parse_hhmm(connection.reindex_at_time)
        if at is not None:
            now_local = now_utc.astimezone(tz)
            nxt_local = now_local.replace(
                hour=at.hour, minute=at.minute, second=0, microsecond=0
            )
            if nxt_local <= now_local:
                nxt_local += timedelta(days=1)
            return nxt_local.astimezone(UTC).replace(tzinfo=None)

    nxt = now_utc + timedelta(minutes=connection.effective_reindex_interval_minutes)
    return nxt.replace(tzinfo=None)
