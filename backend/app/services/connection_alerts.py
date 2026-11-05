"""Alerting when a shared connection's health flips.

The background status sweep (``connection_status_sweep``) re-tests every
``system_only`` connection every few minutes and, through
``ConnectionService._record_probe_outcome``, flips ``Connection.is_active`` on
a sustained change — down only after several consecutive failures, back up on
the first success. This module turns that *edge* into a notification:

  * **Who** — the connection's admins (org-wide ``manage_connections`` and any
    per-connection ``manage_connection`` grant) plus the managers of every data
    source (agent) that relies on the connection. A shared connection going
    down is an org/admin event, so the audience is the people who can fix it or
    whose agents just went blind, never the end users.
  * **How** — an in-app inbox row always (deduped to one live row per
    connection via ``group_key``), plus an email when the org has not switched
    connection-alert email off and, for a *down* alert, the per-connection
    email cooldown has elapsed. Email degrades to inbox-only when no SMTP is
    configured.

Modeled on ``automation_alerts.notify_owner_of_failure``: best-effort
throughout. The probe that triggered this has already committed; an alert that
fails must never be allowed to undo or hide it.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import List

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# How long after a down-alert email before another may go out for the same
# connection. The inbox row is never throttled; this only paces the email so a
# connection that flaps (down → up → down) cannot spam the admins.
_DEFAULT_COOLDOWN_MINUTES = 60

# Org setting (OrganizationSettings.config) that turns connection-alert *email*
# off for the whole org. The in-app inbox row is unaffected. Default on.
EMAIL_ENABLED_KEY = "connection_alerts_email"


def _cooldown() -> timedelta:
    try:
        minutes = max(0, int(os.environ.get("BOW_CONN_DOWN_ALERT_COOLDOWN_MINUTES",
                                            _DEFAULT_COOLDOWN_MINUTES)))
    except (TypeError, ValueError):
        minutes = _DEFAULT_COOLDOWN_MINUTES
    return timedelta(minutes=minutes)


async def _recipient_user_ids(db: AsyncSession, connection) -> List[str]:
    """Union of the connection's admins and the managers of the data sources
    that use it. Deduped; order is not meaningful."""
    from app.core.permission_resolver import (
        get_user_ids_with_permission,
        resolve_permissions,
    )
    from app.models.membership import Membership

    org_id = str(connection.organization_id)
    conn_id = str(connection.id)
    recipients: set[str] = set()

    # Org-wide connection admins (manage_connections) + full admins.
    try:
        recipients.update(
            await get_user_ids_with_permission(db, org_id, "manage_connections", None)
        )
    except Exception:  # noqa: BLE001
        logger.warning("connection alert: resolving org connection admins failed", exc_info=True)

    # Managers of each data source (agent) that relies on this connection.
    for ds in (connection.data_sources or []):
        try:
            recipients.update(
                await get_user_ids_with_permission(db, org_id, "manage", str(ds.id))
            )
        except Exception:  # noqa: BLE001
            logger.warning("connection alert: resolving managers of %s failed", getattr(ds, "id", "?"), exc_info=True)

    # Per-connection `manage_connection` grants are connection-scoped resources,
    # which the data-source-scoped helpers above don't see. Scan members once
    # and pick up anyone holding that grant on *this* connection.
    try:
        rows = (await db.execute(
            select(Membership.user_id).where(and_(
                Membership.organization_id == org_id,
                Membership.user_id.isnot(None),
                Membership.deleted_at.is_(None),
            ))
        )).all()
        for (uid,) in rows:
            uid = str(uid)
            if uid in recipients:
                continue
            resolved = await resolve_permissions(db, uid, org_id)
            if resolved.has_resource_permission("connection", conn_id, "manage_connection"):
                recipients.add(uid)
    except Exception:  # noqa: BLE001
        logger.warning("connection alert: scanning per-connection grants failed", exc_info=True)

    return list(recipients)


async def _email_enabled(db: AsyncSession, org_id: str) -> bool:
    """Whether the org wants connection-alert emails (default True)."""
    from app.models.organization_settings import OrganizationSettings
    try:
        row = (await db.execute(
            select(OrganizationSettings).where(
                OrganizationSettings.organization_id == str(org_id)
            )
        )).scalar_one_or_none()
        if row is None:
            return True
        return bool(row.get_config(EMAIL_ENABLED_KEY, True))
    except Exception:  # noqa: BLE001
        logger.warning("connection alert: reading org email toggle failed", exc_info=True)
        return True


async def notify_connection_health(db: AsyncSession, connection, transition: str) -> None:
    """Deliver a down/restored alert for one shared connection.

    ``transition`` is ``"down"`` (just deactivated) or ``"up"`` (just
    recovered). Inbox always; email when enabled for the org and — for a down
    alert — not inside the per-connection cooldown.
    """
    if transition not in ("down", "up"):
        return

    from app.models.notification import (
        SEVERITY_ERROR, SEVERITY_INFO, SOURCE_CONNECTION,
    )
    from app.services.inbox_service import inbox_service

    org_id = str(connection.organization_id)
    conn_id = str(connection.id)
    name = connection.name or "Connection"
    data_sources = list(connection.data_sources or [])
    link = f"/agents/{data_sources[0].id}" if data_sources else None

    user_ids = await _recipient_user_ids(db, connection)
    if not user_ids:
        logger.info("connection alert: no recipients for connection %s", conn_id)
        return

    if transition == "down":
        ntype = "connection_down"
        severity = SEVERITY_ERROR
        title = f'Connection "{name}" is down'
        body = (
            f'The shared connection "{name}" failed its health checks and has '
            "been marked offline. Agents that rely on it can't query it until "
            "it recovers."
        )
    else:
        ntype = "connection_restored"
        severity = SEVERITY_INFO
        title = f'Connection "{name}" is back online'
        body = f'The shared connection "{name}" reconnected and is active again.'

    # Inbox — always. One live row per connection (dedup via group_key); a
    # restored alert refreshes the same row rather than stacking.
    try:
        await inbox_service.notify_users(
            db,
            organization_id=org_id,
            user_ids=user_ids,
            source=SOURCE_CONNECTION,
            type=ntype,
            title=title,
            body=body,
            severity=severity,
            link=link,
            subject={"kind": "connection", "connection_id": conn_id,
                     "i18n": {"name": name}},
            # Per-type key: a refresh never rewrites a row's ``type`` or
            # downgrades its severity, so down and restored must be separate
            # rows — otherwise a recovery would silently re-surface the old
            # "is down" row still flagged as an error. A repeat of the SAME
            # transition re-surfaces its own row (one live row per connection
            # per direction).
            group_key=f"{ntype}:{conn_id}",
        )
    except Exception:  # noqa: BLE001
        logger.warning("connection alert: inbox delivery failed for %s", conn_id, exc_info=True)

    # Email — gated by the org toggle, and (down only) the cooldown.
    if not await _email_enabled(db, org_id):
        return

    now = datetime.utcnow()
    if transition == "down":
        last = getattr(connection, "last_down_alert_at", None)
        if last is not None and last > now - _cooldown():
            logger.info("connection alert: down-email for %s within cooldown, inbox only", conn_id)
            return
        connection.last_down_alert_at = now
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("connection alert: stamping cooldown failed for %s", conn_id, exc_info=True)

    await _email_recipients(db, connection, user_ids, transition, name, link)


async def _email_recipients(
    db: AsyncSession, connection, user_ids: List[str], transition: str,
    name: str, link: str | None,
) -> None:
    from app.dependencies import _locale_from_org
    from app.models.user import User
    from app.services.notification_service import notification_service
    from app.settings.config import settings

    org_id = str(connection.organization_id)

    try:
        emails = [
            e for (e,) in (await db.execute(
                select(User.email).where(User.id.in_([str(u) for u in user_ids]))
            )).all() if e
        ]
    except Exception:  # noqa: BLE001
        logger.warning("connection alert: resolving recipient emails failed", exc_info=True)
        return
    if not emails:
        return

    base_url = "http://localhost:3000"
    try:
        base_url = getattr(settings.bow_config, "base_url", base_url) or base_url
    except Exception:  # noqa: BLE001
        pass
    full_link = f"{base_url.rstrip('/')}{link}" if link else base_url

    locale = None
    try:
        from app.models.organization import Organization
        organization = await db.get(Organization, org_id)
        if organization is not None:
            locale = _locale_from_org(organization)
    except Exception:  # noqa: BLE001
        locale = None

    try:
        await notification_service.send_connection_status(
            recipient_emails=emails,
            status=transition,
            connection_name=name,
            link=full_link,
            organization_id=org_id,
            locale=locale,
        )
    except Exception:  # noqa: BLE001
        logger.warning("connection alert: email send failed for %s", getattr(connection, "id", "?"), exc_info=True)
