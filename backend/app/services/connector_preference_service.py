"""Per-user chat on/off choices for personal connectors.

A connector (Gmail, Teams, personal Drive, a per-user MCP) joins every run
automatically (``agent_focus_common.resolve_run_agents``). A person can turn
one OFF for chat from the connectors menu; the choice is remembered per
``(user, data_source)`` and read back at run time so a disabled connector
contributes no client, context or tool definitions.

Default is ON: only an explicit OFF is persisted, so a freshly connected
connector works as before and no backfill is needed.
"""
from __future__ import annotations

from typing import Iterable, Optional, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_connector_preference import UserConnectorPreference


async def get_disabled_connector_ids(
    db: AsyncSession,
    user_id: Optional[str],
    organization_id: Optional[str],
    data_source_ids: Optional[Iterable[str]] = None,
) -> Set[str]:
    """Data-source ids this user has turned OFF for chat, in this workspace.

    Only rows with ``enabled = False`` are returned; everything else is ON by
    default. ``data_source_ids`` optionally narrows the query to a known set.
    Returns an empty set for an anonymous caller (no user) — nothing to filter.
    """
    if not user_id or not organization_id:
        return set()
    stmt = select(UserConnectorPreference.data_source_id).where(
        UserConnectorPreference.user_id == str(user_id),
        UserConnectorPreference.organization_id == str(organization_id),
        UserConnectorPreference.enabled.is_(False),
        UserConnectorPreference.deleted_at.is_(None),
    )
    if data_source_ids is not None:
        ids = [str(i) for i in data_source_ids]
        if not ids:
            return set()
        stmt = stmt.where(UserConnectorPreference.data_source_id.in_(ids))
    rows = (await db.execute(stmt)).scalars().all()
    return {str(r) for r in rows}


async def set_connector_enabled(
    db: AsyncSession,
    user_id: str,
    organization_id: str,
    data_source_id: str,
    enabled: bool,
) -> UserConnectorPreference:
    """Upsert the user's on/off choice for one connector. Caller commits."""
    row = (
        await db.execute(
            select(UserConnectorPreference).where(
                UserConnectorPreference.user_id == str(user_id),
                UserConnectorPreference.data_source_id == str(data_source_id),
            )
        )
    ).scalar_one_or_none()
    if row is None:
        row = UserConnectorPreference(
            user_id=str(user_id),
            organization_id=str(organization_id),
            data_source_id=str(data_source_id),
            enabled=bool(enabled),
        )
        db.add(row)
    else:
        row.enabled = bool(enabled)
        row.organization_id = str(organization_id)
        row.deleted_at = None
        db.add(row)
    return row
