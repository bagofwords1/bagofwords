"""Shared application lifecycle and its live agent-access ceiling."""
from datetime import datetime

from sqlalchemy import select, update, or_

from app.errors import AppError, ErrorCode
from app.models.connected_application import ConnectedApplication
from app.models.data_source import DataSource
from app.models.host_application import HostApplication
from app.models.oauth_server import OAuthClient, OAuthAccessToken, OAuthAuthorizationCode


async def get_application(db, organization_id, application_id, *, for_update=False):
    query = select(ConnectedApplication).where(
        ConnectedApplication.id == application_id,
        ConnectedApplication.organization_id == str(organization_id),
        ConnectedApplication.deleted_at.is_(None),
    ).execution_options(populate_existing=True)
    if for_update:
        query = query.with_for_update()
    app = (await db.execute(query)).scalar_one_or_none()
    if app is None:
        raise AppError.not_found(ErrorCode.APPLICATION_NOT_FOUND, 'Application not found')
    return app


async def attach_application(db, organization_id, application_id, model):
    app = await get_application(db, organization_id, application_id, for_update=True)
    if await db.get(model, app.id) is not None:
        raise AppError.conflict(ErrorCode.APPLICATION_CAPABILITY_EXISTS, 'This application already has this capability')
    return app


async def validate_agents(db, organization_id, ids):
    if ids is None:
        return None
    wanted = list(dict.fromkeys(ids))
    found = set((await db.execute(select(DataSource.id).where(
        DataSource.id.in_(wanted), DataSource.organization_id == str(organization_id),
        DataSource.deleted_at.is_(None), DataSource.is_active.is_(True),
    ))).scalars().all())
    if found != set(wanted):
        raise AppError.bad_request(ErrorCode.APPLICATION_AGENTS_INVALID, 'Select existing agents in this organization')
    return wanted


async def revoke_credentials(db, app):
    """Disabling/deleting is irreversible for existing sessions, including refresh."""
    oauth = await db.get(OAuthClient, app.id)
    host = await db.get(HostApplication, app.id)
    now = datetime.utcnow()
    predicates = [OAuthAccessToken.host_application_id == app.id]
    if oauth:
        predicates.append(OAuthAccessToken.client_id == oauth.client_id)
        await db.execute(update(OAuthAuthorizationCode).where(
            OAuthAuthorizationCode.client_id == oauth.client_id,
            OAuthAuthorizationCode.deleted_at.is_(None),
        ).values(deleted_at=now))
    await db.execute(update(OAuthAccessToken).where(
        or_(*predicates), OAuthAccessToken.deleted_at.is_(None),
    ).values(deleted_at=now))
    if host:
        host.epoch = int(host.epoch or 1) + 1
        host.revision = int(host.revision or 1) + 1


async def agent_ceiling(db, organization_id):
    """None means unrestricted; an empty set means no agents. Fail closed."""
    from app.core.request_origin import current
    origin = current()
    if origin is None:
        return None
    app = (await db.execute(select(ConnectedApplication).where(
        ConnectedApplication.id == origin[1],
        ConnectedApplication.organization_id == str(organization_id),
        ConnectedApplication.deleted_at.is_(None), ConnectedApplication.enabled.is_(True),
    ).execution_options(populate_existing=True))).scalar_one_or_none()
    if app is None:
        return set()
    return None if app.agent_ids is None else set(app.agent_ids)


async def filter_agents(db, agents, organization_id):
    ceiling = await agent_ceiling(db, organization_id)
    return list(agents or []) if ceiling is None else [a for a in (agents or []) if str(a.id) in ceiling]
