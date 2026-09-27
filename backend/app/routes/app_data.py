"""Artifact app data endpoints (records behind `useCollection`).

`{artifact_id}` is the PARENT artifact id (`ArtifactSchema.artifact_id`),
never a version id. Authentication is optional: anonymous visitors of a public
artifact may read owner-written collections. The organization comes from the
artifact, never from `X-Organization-Id`, and every access decision is made in
`app_data_service` (Layer 1 report visibility, then the collection rules).
"""
from typing import Optional

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import current_user_optional
from app.dependencies import get_async_db
from app.models.user import User
from app.schemas.app_storage import (
    AppRecordCreate,
    AppRecordDelete,
    AppRecordDeleted,
    AppRecordList,
    AppRecordOut,
    AppRecordUpdate,
)
from app.services.app_data_service import app_data_service

router = APIRouter(prefix="/artifacts", tags=["app_data"])


@router.get("/{artifact_id}/data/{collection}", response_model=AppRecordList)
async def list_app_records(
    artifact_id: str,
    collection: str,
    db: AsyncSession = Depends(get_async_db),
    user: Optional[User] = Depends(current_user_optional),
):
    return await app_data_service.list_records(db, artifact_id=artifact_id, collection=collection, user=user)


@router.post("/{artifact_id}/data/{collection}", response_model=AppRecordOut, status_code=201)
async def create_app_record(
    artifact_id: str,
    collection: str,
    payload: AppRecordCreate,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    user: Optional[User] = Depends(current_user_optional),
):
    return await app_data_service.create_record(
        db, artifact_id=artifact_id, collection=collection, user=user, payload=payload, request=request,
    )


@router.patch("/{artifact_id}/data/{collection}/{record_id}", response_model=AppRecordOut)
async def update_app_record(
    artifact_id: str,
    collection: str,
    record_id: str,
    payload: AppRecordUpdate,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    user: Optional[User] = Depends(current_user_optional),
):
    return await app_data_service.update_record(
        db, artifact_id=artifact_id, collection=collection, record_id=record_id, user=user,
        payload=payload, request=request,
    )


@router.delete("/{artifact_id}/data/{collection}/{record_id}", response_model=AppRecordDeleted)
async def delete_app_record(
    artifact_id: str,
    collection: str,
    record_id: str,
    payload: AppRecordDelete,
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    user: Optional[User] = Depends(current_user_optional),
):
    return await app_data_service.delete_record(
        db, artifact_id=artifact_id, collection=collection, record_id=record_id, user=user,
        payload=payload, request=request,
    )
