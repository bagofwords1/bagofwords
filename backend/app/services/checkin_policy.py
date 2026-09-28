"""Agent check-in guardrails — code only, facts not judgments.

Every rule here is deterministic: a setting, a count, a clock, an access
check. Whether a follow-up is *worth* running is the judge's call
(``app.ai.agents.checkins.judge``); this module only decides whether it is
*allowed* to. Thresholds come from org settings or the module constants below.

All datetimes are naive UTC (the app's storage convention) unless a function
says otherwise; ``now`` is injectable everywhere so tests control the clock.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_checkin import (
    AgentCheckin,
    RUN_STATUSES,
    STATUS_PLANNED,
    STATUS_RUNNING,
    REASON_ACCESS_LOST,
    REASON_ORG_DAILY_CAP,
    REASON_PENDING_REPORT,
    REASON_PENDING_USER,
    REASON_REPORT_DELETED,
    REASON_WEEKLY_CAP,
)

SETTING_ENABLED = "enable_agent_checkins"
SETTING_WEEKLY_CAP = "checkins_max_per_user_per_week"
SETTING_ORG_DAILY_CAP = "checkins_max_runs_per_org_per_day"

DEFAULT_WEEKLY_CAP = 2
DEFAULT_ORG_DAILY_CAP = 50

MAX_PENDING_PER_USER = 1
MAX_PENDING_PER_REPORT = 1

MIN_DUE = timedelta(hours=2)
MAX_DUE = timedelta(days=14)

WORK_START_HOUR = 9
WORK_END_HOUR = 18          # exclusive: 17:59 is inside, 18:00 is outside
WORK_DAYS = {0, 1, 2, 3, 4}  # Mon..Fri
MAX_JITTER_MINUTES = 90

# A live agent run in the report at fire time re-arms the check-in this far out.
LIVE_RUN_REARM = timedelta(minutes=30)

# Relative change the check-in run treats as "material" (see prompts.py).
MATERIAL_CHANGE_PCT = 10


# ── settings ────────────────────────────────────────────────────────────────

def _cfg_value(org_settings: Any, key: str, default: Any) -> Any:
    if org_settings is None:
        return default
    try:
        cfg = org_settings.get_config(key)
    except Exception:
        return default
    if cfg is None:
        return default
    return getattr(cfg, "value", cfg)


def feature_enabled(org_settings: Any) -> bool:
    """The master switch. Off by default while the feature is in lab."""
    return bool(_cfg_value(org_settings, SETTING_ENABLED, False))


def _int_setting(org_settings: Any, key: str, default: int) -> int:
    v = _cfg_value(org_settings, key, default)
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return default


def weekly_cap(org_settings: Any) -> int:
    return _int_setting(org_settings, SETTING_WEEKLY_CAP, DEFAULT_WEEKLY_CAP)


def org_daily_cap(org_settings: Any) -> int:
    return _int_setting(org_settings, SETTING_ORG_DAILY_CAP, DEFAULT_ORG_DAILY_CAP)


async def load_org_settings(db: AsyncSession, organization_id: str):
    """Fresh read of the org's settings row (never a cached ORM instance), so a
    setting flipped since the job was armed is respected."""
    from app.models.organization_settings import OrganizationSettings

    return (
        await db.execute(
            select(OrganizationSettings).where(
                OrganizationSettings.organization_id == str(organization_id)
            ).execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


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


async def user_opted_out(db: AsyncSession, organization_id: str, user_id: str) -> bool:
    """The user turned check-ins off for themselves in this org (profile)."""
    from app.models.membership import Membership

    v = (
        await db.execute(
            select(Membership.checkins_opt_out).where(
                Membership.organization_id == str(organization_id),
                Membership.user_id == str(user_id),
                Membership.deleted_at.is_(None),
            ).execution_options(populate_existing=True)
        )
    ).scalars().first()
    return bool(v)


# ── time ────────────────────────────────────────────────────────────────────

def clamp_due(now: datetime, due: datetime) -> datetime:
    """Clamp ``due`` into [now + 2h, now + 14d]."""
    lo, hi = now + MIN_DUE, now + MAX_DUE
    return min(max(due, lo), hi)


def in_working_window(due_utc: datetime, tz_name: str) -> bool:
    local = due_utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(tz_name))
    return local.weekday() in WORK_DAYS and WORK_START_HOUR <= local.hour < WORK_END_HOUR


def shift_into_working_window(
    due_utc: datetime, tz_name: str, *, rng: Optional[random.Random] = None,
) -> datetime:
    """Return ``due_utc`` if it falls Mon–Fri 09:00–18:00 in ``tz_name``;
    otherwise the next window start plus 0–90 minutes of jitter.

    Works in local wall-clock time and converts back through the zone, so a
    DST change between now and the window start lands on 09:00 *local*.
    Input and output are naive UTC.
    """
    tz = ZoneInfo(tz_name)
    if in_working_window(due_utc, tz_name):
        return due_utc
    local = due_utc.replace(tzinfo=timezone.utc).astimezone(tz)
    day = local.date()
    if local.weekday() in WORK_DAYS and local.hour < WORK_START_HOUR:
        target_day = day
    else:
        target_day = day + timedelta(days=1)
    while target_day.weekday() not in WORK_DAYS:
        target_day += timedelta(days=1)
    start_local = datetime(
        target_day.year, target_day.month, target_day.day, WORK_START_HOUR, 0, tzinfo=tz,
    )
    jitter = (rng or random).randint(0, MAX_JITTER_MINUTES)
    shifted = start_local + timedelta(minutes=jitter)
    return shifted.astimezone(timezone.utc).replace(tzinfo=None)


def plan_due_at(
    now: datetime, due_in_hours: float, tz_name: str, *, rng: Optional[random.Random] = None,
) -> datetime:
    """The planner's relative due time → a clamped, working-hours UTC datetime."""
    raw = now + timedelta(hours=float(due_in_hours))
    return shift_into_working_window(clamp_due(now, raw), tz_name, rng=rng)


# ── counts ──────────────────────────────────────────────────────────────────

async def _count(db: AsyncSession, *where) -> int:
    return int((await db.execute(select(func.count(AgentCheckin.id)).where(*where))).scalar() or 0)


async def pending_for_user(db: AsyncSession, organization_id: str, user_id: str) -> int:
    return await _count(
        db,
        AgentCheckin.organization_id == str(organization_id),
        AgentCheckin.user_id == str(user_id),
        AgentCheckin.status == STATUS_PLANNED,
    )


async def pending_for_report(db: AsyncSession, report_id: str) -> int:
    return await _count(
        db, AgentCheckin.report_id == str(report_id), AgentCheckin.status == STATUS_PLANNED,
    )


async def runs_for_user_since(
    db: AsyncSession, organization_id: str, user_id: str, since: datetime,
    exclude_id: Optional[str] = None,
) -> int:
    where = [
        AgentCheckin.organization_id == str(organization_id),
        AgentCheckin.user_id == str(user_id),
        AgentCheckin.status.in_((*RUN_STATUSES, STATUS_RUNNING)),
        AgentCheckin.judged_at >= since,
    ]
    if exclude_id:
        where.append(AgentCheckin.id != str(exclude_id))
    return await _count(db, *where)


async def runs_for_org_since(
    db: AsyncSession, organization_id: str, since: datetime, exclude_id: Optional[str] = None,
) -> int:
    where = [
        AgentCheckin.organization_id == str(organization_id),
        AgentCheckin.status.in_((*RUN_STATUSES, STATUS_RUNNING)),
        AgentCheckin.judged_at >= since,
    ]
    if exclude_id:
        where.append(AgentCheckin.id != str(exclude_id))
    return await _count(db, *where)


# ── rule sets ───────────────────────────────────────────────────────────────

async def check_plan_limits(
    db: AsyncSession, *, organization_id: str, user_id: str, report_id: str,
    org_settings: Any, now: Optional[datetime] = None,
) -> Optional[str]:
    """Limits checked when planning. Returns a reason code, or None if allowed."""
    now = now or datetime.utcnow()
    if await pending_for_user(db, organization_id, user_id) >= MAX_PENDING_PER_USER:
        return REASON_PENDING_USER
    if await pending_for_report(db, report_id) >= MAX_PENDING_PER_REPORT:
        return REASON_PENDING_REPORT
    if await runs_for_user_since(db, organization_id, user_id, now - timedelta(days=7)) >= weekly_cap(org_settings):
        return REASON_WEEKLY_CAP
    return None


async def check_fire_limits(
    db: AsyncSession, checkin: AgentCheckin, org_settings: Any, now: Optional[datetime] = None,
) -> Optional[str]:
    """Run caps checked when the job fires (excluding the check-in itself)."""
    now = now or datetime.utcnow()
    if await runs_for_user_since(
        db, checkin.organization_id, checkin.user_id, now - timedelta(days=7), exclude_id=checkin.id,
    ) >= weekly_cap(org_settings):
        return REASON_WEEKLY_CAP
    if await runs_for_org_since(
        db, checkin.organization_id, now - timedelta(days=1), exclude_id=checkin.id,
    ) >= org_daily_cap(org_settings):
        return REASON_ORG_DAILY_CAP
    return None


async def check_access(db: AsyncSession, checkin: AgentCheckin) -> Optional[str]:
    """Report still exists and the user may still run a turn in it.

    Mirrors the completion route's gate (``POST /reports/{id}/completions``:
    active membership, ``create_reports``, report owner) and adds the report's
    data sources, which the run will query as this user.
    """
    from sqlalchemy.orm import selectinload, lazyload

    from app.core.permission_resolver import (
        principal_belongs_to_org,
        resolve_permissions,
        user_can_access_data_source,
    )
    from app.models.report import Report
    from app.models.user import User

    report = (
        await db.execute(
            select(Report)
            .options(lazyload("*"), selectinload(Report.data_sources))
            .where(Report.id == str(checkin.report_id))
        )
    ).scalar_one_or_none()
    if (
        report is None
        or report.deleted_at is not None
        or getattr(report, "status", None) == "archived"
        or str(report.organization_id) != str(checkin.organization_id)
    ):
        return REASON_REPORT_DELETED

    user = await db.get(User, str(checkin.user_id))
    if user is None or not getattr(user, "is_active", True):
        return REASON_ACCESS_LOST
    if not await principal_belongs_to_org(db, user, checkin.organization_id):
        return REASON_ACCESS_LOST
    if str(report.user_id) != str(user.id):
        return REASON_ACCESS_LOST
    resolved = await resolve_permissions(db, str(user.id), str(checkin.organization_id))
    if not resolved.has_org_permission("create_reports"):
        return REASON_ACCESS_LOST
    for ds in report.data_sources or []:
        if not await user_can_access_data_source(db, str(user.id), str(checkin.organization_id), ds):
            return REASON_ACCESS_LOST
    return None


async def live_run_in_report(db: AsyncSession, report_id: str) -> bool:
    """An agent turn is in flight in this report right now."""
    from app.models.completion import Completion

    n = (
        await db.execute(
            select(func.count(Completion.id)).where(
                Completion.report_id == str(report_id),
                Completion.role == "system",
                Completion.status == "in_progress",
            )
        )
    ).scalar()
    return bool(n)
