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
  * **When** — off ``Connection.health_alert_state`` (what admins were last
    told), not the probe that flipped ``is_active``: a connection that is
    inactive but not yet announced is announced as down, and one that is
    active again after an announced outage is announced as restored. So a
    manual "Test connection" that flips the flag can't swallow the alert.
  * **How** — an in-app inbox row always (one live row per connection; the
    opposite-direction row is cleared), plus an email when both the org and
    the connection allow it. A down email is paced by a per-connection
    cooldown; a restored email goes out only when that outage's down email
    did, so a flapping source can't send a stream of "back online" mails.

Modeled on ``automation_alerts.notify_owner_of_failure``: best-effort
throughout. The probe that triggered this has already committed; an alert that
fails must never be allowed to undo or hide it.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import and_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

# How long after a down-alert email before another may go out for the same
# connection. The inbox row is never throttled; this only paces the email so a
# connection that flaps (down → up → down) cannot spam the admins.
_DEFAULT_COOLDOWN_MINUTES = 60

# Org setting (OrganizationSettings.config) that turns connection-alert *email*
# on for the whole org. The in-app inbox row is unaffected. Default off: an
# admin opts in from Settings → Email; until then only the inbox row fires.
EMAIL_ENABLED_KEY = "connection_alerts_email"

# Where a recipient who manages none of the connection's agents is sent: the
# agents list, where connections are managed.
_FALLBACK_LINK = "/agents"


def _cooldown() -> timedelta:
    try:
        minutes = max(0, int(os.environ.get("BOW_CONN_DOWN_ALERT_COOLDOWN_MINUTES",
                                            _DEFAULT_COOLDOWN_MINUTES)))
    except (TypeError, ValueError):
        minutes = _DEFAULT_COOLDOWN_MINUTES
    return timedelta(minutes=minutes)


HEALTH_DOWN = "down"
HEALTH_DOWN_EMAILED = "down_emailed"
_ANNOUNCED_DOWN = (HEALTH_DOWN, HEALTH_DOWN_EMAILED)


def pending_transition(connection) -> Optional[str]:
    """The alert this connection still owes: ``"down"`` when it is inactive but
    no outage has been announced, ``"up"`` when it is active again after an
    announced outage, else ``None``."""
    announced_down = getattr(connection, "health_alert_state", None) in _ANNOUNCED_DOWN
    if not connection.is_active and not announced_down:
        return "down"
    if connection.is_active and announced_down:
        return "up"
    return None


async def _recipient_links(db: AsyncSession, connection) -> Dict[str, str]:
    """Who to alert, and where each recipient's link should point.

    Recipients are the connection's admins (org ``manage_connections`` or a
    per-connection ``manage_connection`` grant) and the managers of the agents
    that use it. Each link goes to an agent that recipient can manage, else to
    the agents list where connections are managed — never to an agent the
    recipient can't open. One permission resolution per member (the helpers in
    ``permission_resolver`` would each re-scan every member)."""
    from app.core.permission_resolver import resolve_permissions
    from app.models.membership import Membership

    org_id = str(connection.organization_id)
    conn_id = str(connection.id)
    ds_ids = [str(ds.id) for ds in (connection.data_sources or [])]

    rows = (await db.execute(
        select(Membership.user_id).where(and_(
            Membership.organization_id == org_id,
            Membership.user_id.isnot(None),
            Membership.deleted_at.is_(None),
        ))
    )).all()

    out: Dict[str, str] = {}
    for uid in {str(u) for (u,) in rows}:
        try:
            resolved = await resolve_permissions(db, uid, org_id)
        except Exception:  # noqa: BLE001
            logger.warning("connection alert: resolving permissions for %s failed", uid, exc_info=True)
            continue
        managed = [d for d in ds_ids if resolved.has_resource_permission("data_source", d, "manage")]
        if (
            managed
            or resolved.has_org_permission("manage_connections")
            or resolved.has_resource_permission("connection", conn_id, "manage_connection")
        ):
            out[uid] = f"/agents/{managed[0]}" if managed else _FALLBACK_LINK
    return out


async def org_email_enabled(db: AsyncSession, org_id: str) -> bool:
    """Whether the org wants connection-alert emails (default False).

    Emails are opt-in: an admin flips the toggle in Settings → Email. A missing
    row, a missing key, or a read error all mean "not opted in", so we stay
    silent rather than surprise an org with mail it never asked for."""
    from app.models.organization_settings import OrganizationSettings
    try:
        row = (await db.execute(
            select(OrganizationSettings).where(
                OrganizationSettings.organization_id == str(org_id)
            )
        )).scalar_one_or_none()
        if row is None:
            return False
        return bool(row.get_config(EMAIL_ENABLED_KEY, False))
    except Exception:  # noqa: BLE001
        logger.warning("connection alert: reading org email toggle failed", exc_info=True)
        return False


async def _clear_opposite_rows(db: AsyncSession, conn_id: str, ntype: str) -> None:
    """Retire the other direction's inbox rows for this connection, so a
    recovery doesn't leave a live "is down" error behind (and a new outage
    doesn't leave a stale "back online")."""
    from app.models.notification import Notification, SOURCE_CONNECTION

    other = "connection_restored" if ntype == "connection_down" else "connection_down"
    await db.execute(
        update(Notification)
        .where(and_(
            Notification.source == SOURCE_CONNECTION,
            Notification.group_key == f"{other}:{conn_id}",
            Notification.deleted_at.is_(None),
        ))
        .values(deleted_at=datetime.utcnow())
    )
    await db.commit()


async def notify_connection_health(db: AsyncSession, connection) -> None:
    """Send whatever down/restored alert this shared connection still owes.

    Call after an automated probe has committed. Inbox always; email when the
    org and the connection allow it, paced as described in the module doc.
    """
    transition = pending_transition(connection)
    if transition is None:
        return

    from app.models.notification import (
        SEVERITY_ERROR, SEVERITY_INFO, SOURCE_CONNECTION,
    )
    from app.services.inbox_service import inbox_service

    org_id = str(connection.organization_id)
    conn_id = str(connection.id)
    name = connection.name or "Connection"
    previous_state = getattr(connection, "health_alert_state", None)

    # Record the announcement first: if delivery fails part-way, the next probe
    # must not announce the same outage again.
    connection.health_alert_state = HEALTH_DOWN if transition == "down" else None
    await db.commit()

    links = await _recipient_links(db, connection)
    if not links:
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

    # Inbox — always. One live row per connection per direction; the other
    # direction's row is retired. ``resurface_after_hours=0`` so a user who
    # dismissed an earlier outage's row still hears about the next one.
    try:
        await _clear_opposite_rows(db, conn_id, ntype)
        for link, user_ids in _group_by_link(links).items():
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
                group_key=f"{ntype}:{conn_id}",
                resurface_after_hours=0,
            )
    except Exception:  # noqa: BLE001
        logger.warning("connection alert: inbox delivery failed for %s", conn_id, exc_info=True)

    # Email — the org toggle is the master gate, the per-connection switch can
    # only mute under it (effective = org AND connection).
    connection_enabled = getattr(connection, "alert_emails_enabled", True)
    if not (await org_email_enabled(db, org_id) and connection_enabled is not False):
        return

    now = datetime.utcnow()
    if transition == "down":
        last = getattr(connection, "last_down_alert_at", None)
        if last is not None and last > now - _cooldown():
            logger.info("connection alert: down-email for %s within cooldown, inbox only", conn_id)
            return
    elif previous_state != HEALTH_DOWN_EMAILED:
        # Admins were never emailed about this outage; don't email the recovery.
        return

    sent = await _email_recipients(db, connection, links, transition, name)
    if sent and transition == "down":
        # Stamp only after a send went out, so a failed or unconfigured send
        # doesn't silence the next outage's email.
        connection.last_down_alert_at = now
        connection.health_alert_state = HEALTH_DOWN_EMAILED
        try:
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.warning("connection alert: stamping email state failed for %s", conn_id, exc_info=True)


def _group_by_link(links: Dict[str, str]) -> Dict[str, List[str]]:
    grouped: Dict[str, List[str]] = {}
    for uid, link in links.items():
        grouped.setdefault(link, []).append(uid)
    return grouped


async def _email_recipients(
    db: AsyncSession, connection, links: Dict[str, str], transition: str, name: str,
) -> bool:
    """Email each recipient (grouped by their link). True if any send went out."""
    from app.dependencies import _locale_from_org
    from app.models.user import User
    from app.services.notification_service import notification_service
    from app.settings.config import settings

    org_id = str(connection.organization_id)

    try:
        email_by_uid = {
            str(uid): e for (uid, e) in (await db.execute(
                select(User.id, User.email).where(User.id.in_(list(links)))
            )).all() if e
        }
    except Exception:  # noqa: BLE001
        logger.warning("connection alert: resolving recipient emails failed", exc_info=True)
        return False
    if not email_by_uid:
        return False

    base_url = "http://localhost:3000"
    try:
        base_url = getattr(settings.bow_config, "base_url", base_url) or base_url
    except Exception:  # noqa: BLE001
        pass

    locale = None
    try:
        from app.models.organization import Organization
        organization = await db.get(Organization, org_id)
        if organization is not None:
            locale = _locale_from_org(organization)
    except Exception:  # noqa: BLE001
        locale = None

    sent = False
    for link, user_ids in _group_by_link(links).items():
        emails = [email_by_uid[u] for u in user_ids if u in email_by_uid]
        if not emails:
            continue
        try:
            ok = await notification_service.send_connection_status(
                recipient_emails=emails,
                status=transition,
                connection_name=name,
                link=f"{base_url.rstrip('/')}{link}",
                organization_id=org_id,
                locale=locale,
            )
            sent = sent or bool(ok)
        except Exception:  # noqa: BLE001
            logger.warning("connection alert: email send failed for %s", getattr(connection, "id", "?"), exc_info=True)
    return sent
