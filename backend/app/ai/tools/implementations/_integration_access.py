"""Shared resolution for per-user (agent-less) connection access.

Phase 0 of the Integrations design: a connection is usable in a turn when it is
either attached to the report's agent (the existing path) OR personally connected
by the current user (`UserConnectionCredentials`). This module centralizes the
"connections this user may use right now" lookup so the MCP/tool path
(search_mcps / execute_mcp) and the file path resolve identically.
"""
from __future__ import annotations

from typing import Dict, Iterable, Optional, Set

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def user_personal_connection_ids(
    db: AsyncSession,
    organization,
    current_user,
    types: Optional[Iterable[str]] = None,
) -> Set[str]:
    """Connection ids the current user has personally connected (active creds).

    Optionally filtered to `types` (e.g. {"mcp", "custom_api", "gmail"}). Returns
    an empty set when there's no user/org context.
    """
    if not (db and organization and current_user):
        return set()
    from app.models.connection import Connection
    from app.models.user_connection_credentials import UserConnectionCredentials

    cred_ids = (await db.execute(
        select(UserConnectionCredentials.connection_id).where(
            UserConnectionCredentials.user_id == str(current_user.id),
            UserConnectionCredentials.is_active == True,  # noqa: E712
        )
    )).scalars().all()
    if not cred_ids:
        return set()

    q = select(Connection.id).where(
        Connection.id.in_([str(c) for c in cred_ids]),
        Connection.organization_id == str(organization.id),
        Connection.is_active == True,  # noqa: E712
    )
    if types is not None:
        q = q.where(Connection.type.in_(list(types)))
    return {str(cid) for cid in (await db.execute(q)).scalars().all()}


async def user_personal_connection_info(
    db: AsyncSession,
    organization,
    current_user,
    types: Optional[Iterable[str]] = None,
) -> Dict[str, dict]:
    """Same as above but returns {connection_id: {name, type}} for display."""
    ids = await user_personal_connection_ids(db, organization, current_user, types)
    if not ids:
        return {}
    from app.models.connection import Connection

    rows = (await db.execute(
        select(Connection).where(Connection.id.in_(list(ids)))
    )).scalars().all()
    return {str(r.id): {"name": r.name, "type": r.type} for r in rows}
