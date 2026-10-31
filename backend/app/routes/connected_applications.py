"""Administrators manage one identity for an external application's capabilities."""
from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.dependencies import get_async_db, get_current_organization
from app.models.connected_application import ConnectedApplication
from app.models.host_application import HostApplication
from app.models.oauth_server import OAuthClient
from app.models.organization import Organization
from app.models.user import User
from app.routes.host_applications import audit
from app.schemas.connected_application_schema import ApplicationCreate, ApplicationUpdate
from app.services.connected_application_service import get_application, validate_agents, revoke_credentials

router = APIRouter(prefix='/connected_applications', tags=['connected applications'])


async def payload(db, app):
    return dict(id=app.id, name=app.name, enabled=app.enabled, agent_ids=app.agent_ids,
                oauth=await db.get(OAuthClient, app.id) is not None,
                embedding=await db.get(HostApplication, app.id) is not None,
                created_at=app.created_at)


@router.get('')
@requires_permission('manage_settings')
async def list_applications(current_user: User = Depends(current_user),
                            organization: Organization = Depends(get_current_organization),
                            db: AsyncSession = Depends(get_async_db)):
    apps = (await db.execute(select(ConnectedApplication).where(
        ConnectedApplication.organization_id == str(organization.id),
        ConnectedApplication.deleted_at.is_(None),
    ).order_by(ConnectedApplication.created_at))).scalars().all()
    return [await payload(db, app) for app in apps]


@router.post('', status_code=201)
@requires_permission('manage_settings')
async def create_application(body: ApplicationCreate, request: Request,
                             current_user: User = Depends(current_user),
                             organization: Organization = Depends(get_current_organization),
                             db: AsyncSession = Depends(get_async_db)):
    ids = await validate_agents(db, organization.id, body.agent_ids)
    app = ConnectedApplication(name=body.name, organization_id=str(organization.id),
                               agent_ids=ids, created_by_user_id=str(current_user.id))
    db.add(app)
    await db.commit()
    await db.refresh(app)
    await audit(db, organization.id, 'connected_application.created', user_id=str(current_user.id),
                resource_type='connected_application', resource_id=app.id, request=request)
    return await payload(db, app)


@router.patch('/{application_id}')
@requires_permission('manage_settings')
async def update_application(application_id: str, body: ApplicationUpdate, request: Request,
                             current_user: User = Depends(current_user),
                             organization: Organization = Depends(get_current_organization),
                             db: AsyncSession = Depends(get_async_db)):
    app = await get_application(db, organization.id, application_id, for_update=True)
    changes = body.model_dump(exclude_unset=True)
    if 'agent_ids' in changes:
        changes['agent_ids'] = await validate_agents(db, organization.id, changes['agent_ids'])
    if changes.get('enabled') is False:
        await revoke_credentials(db, app)
    for key, value in changes.items():
        setattr(app, key, value)
    await db.commit()
    await db.refresh(app)
    await audit(db, organization.id, 'connected_application.updated', user_id=str(current_user.id),
                resource_type='connected_application', resource_id=app.id, details=changes, request=request)
    return await payload(db, app)


@router.delete('/{application_id}', status_code=204)
@requires_permission('manage_settings')
async def delete_application(application_id: str, request: Request,
                             current_user: User = Depends(current_user),
                             organization: Organization = Depends(get_current_organization),
                             db: AsyncSession = Depends(get_async_db)):
    app = await get_application(db, organization.id, application_id, for_update=True)
    await revoke_credentials(db, app)
    app.enabled = False
    app.deleted_at = datetime.utcnow()
    await db.commit()
    await audit(db, organization.id, 'connected_application.deleted', user_id=str(current_user.id),
                resource_type='connected_application', resource_id=application_id, request=request)
    return Response(status_code=204)
