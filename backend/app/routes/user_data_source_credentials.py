from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db
from app.core.auth import current_user
from app.dependencies import get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.core.permissions_decorator import requires_permission

from sqlalchemy import select

from app.schemas.user_data_source_credentials_schema import (
    UserDataSourceCredentialsCreate,
    UserDataSourceCredentialsUpdate,
    UserDataSourceCredentialsSchema,
)
from app.services.user_data_source_credentials_service import UserDataSourceCredentialsService


router = APIRouter(tags=["data_sources"])
svc = UserDataSourceCredentialsService()


async def _load_datasource(db: AsyncSession, organization: Organization, data_source_id: str) -> DataSource:
    from app.models.data_source import DataSource
    res = await db.execute(select(DataSource).where(DataSource.id == data_source_id, DataSource.organization_id == organization.id))
    ds = res.scalar_one_or_none()
    if not ds:
        raise HTTPException(status_code=404, detail="Data source not found")
    return ds


@router.get("/data_sources/{data_source_id}/my-credentials", response_model=UserDataSourceCredentialsSchema | None)
@requires_permission('view_data_source', model=DataSource)
async def get_my_credentials(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    ds = await _load_datasource(db, organization, data_source_id)
    return await svc.get_my_credentials(db=db, data_source=ds, user=current_user)


@router.post("/data_sources/{data_source_id}/my-credentials", response_model=UserDataSourceCredentialsSchema)
@requires_permission('view_data_source', model=DataSource)
async def upsert_my_credentials(
    data_source_id: str,
    payload: UserDataSourceCredentialsCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    ds = await _load_datasource(db, organization, data_source_id)
    return await svc.upsert_my_credentials(db=db, data_source=ds, user=current_user, payload=payload)


@router.patch("/data_sources/{data_source_id}/my-credentials", response_model=UserDataSourceCredentialsSchema)
@requires_permission('view_data_source', model=DataSource)
async def patch_my_credentials(
    data_source_id: str,
    payload: UserDataSourceCredentialsUpdate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    ds = await _load_datasource(db, organization, data_source_id)
    return await svc.patch_my_credentials(db=db, data_source=ds, user=current_user, payload=payload)


@router.delete("/data_sources/{data_source_id}/my-credentials", status_code=204)
@requires_permission('view_data_source', model=DataSource)
async def delete_my_credentials(
    data_source_id: str,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    ds = await _load_datasource(db, organization, data_source_id)
    await svc.delete_my_credentials(db=db, data_source=ds, user=current_user)
    return None


@router.post("/data_sources/{data_source_id}/my-credentials/test", response_model=dict)
@requires_permission('view_data_source', model=DataSource)
async def test_my_credentials(
    data_source_id: str,
    payload: UserDataSourceCredentialsCreate,
    db: AsyncSession = Depends(get_async_db),
    current_user: User = Depends(current_user),
    organization: Organization = Depends(get_current_organization),
):
    ds = await _load_datasource(db, organization, data_source_id)
    return await svc.test_my_credentials(db=db, data_source=ds, user=current_user, payload=payload)


