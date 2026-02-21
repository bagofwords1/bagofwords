from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session
from sqlalchemy import select
from app.dependencies import get_db
from typing import Optional
import os

from app.services.file_service import FileService
from app.schemas.file_schema import FileSchema, FileSchemaWithMetadata, FileSchemaWithCompletionId
from app.models.user import User
from app.models.file import File as FileModel
from app.core.auth import current_user
from app.models.organization import Organization
from app.dependencies import get_current_organization
from fastapi import Form
from app.core.permissions_decorator import requires_permission
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from app.models.report import Report

router = APIRouter(tags=["files"])
file_service = FileService()

@router.post("/files", response_model=FileSchema)
@requires_permission('upload_files')
async def upload_file(file: UploadFile = File(...), report_id: Optional[str] = Form(None), current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await file_service.upload_file(db, file, current_user, organization, report_id)

@router.get("/reports/{report_id}/files", response_model=list[FileSchemaWithCompletionId])
@requires_permission('view_files', model=Report)
async def get_files_by_report(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await file_service.get_files_by_report(db, report_id, organization)

@router.delete("/reports/{report_id}/files/{file_id}")
@requires_permission('delete_files', model=Report)
async def remove_file_from_report(file_id: str, report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await file_service.remove_file_from_report(db, file_id, report_id, organization, current_user)

@router.get("/files", response_model=list[FileSchemaWithMetadata])
@requires_permission('view_files')
async def get_files(current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await file_service.get_files(db, organization)

@router.get("/files/{file_id}/content")
@requires_permission('view_files')
async def get_file_content(file_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    """Serve file content (for displaying images in chat)."""
    stmt = select(FileModel).filter(FileModel.id == file_id, FileModel.organization_id == organization.id)
    result = await db.execute(stmt)
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    if not file.path or not os.path.exists(file.path):
        raise HTTPException(status_code=404, detail="File content not found")

    return FileResponse(
        path=file.path,
        filename=file.filename,
        media_type=file.content_type
    )