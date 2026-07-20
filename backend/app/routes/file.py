from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Request
from fastapi.responses import FileResponse, Response
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
from app.core.permissions_decorator import requires_permission, requires_resource_permission
from sqlalchemy.ext.asyncio import AsyncSession
from app.dependencies import get_async_db
from app.models.report import Report
from app.ee.audit.service import audit_service

router = APIRouter(tags=["files"])
file_service = FileService()

@router.post("/files", response_model=FileSchema)
@requires_permission('manage_files')
async def upload_file(request: Request, file: UploadFile = File(...), report_id: Optional[str] = Form(None), data_source_id: Optional[str] = Form(None), current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    result = await file_service.upload_file(db, file, current_user, organization, report_id, data_source_id)
    try:
        await audit_service.log(
            db=db,
            organization_id=organization.id,
            action="file.uploaded",
            user_id=current_user.id,
            resource_type="file",
            resource_id=result.id,
            details={"filename": file.filename, "content_type": file.content_type, "data_source_id": data_source_id},
            request=request,
        )
    except Exception:
        pass
    # Silent session event: user uploaded a file to this report (report-scoped
    # uploads only — data-source uploads are not part of a conversation).
    if report_id:
        from types import SimpleNamespace
        from app.services.session_event_service import SessionEventService
        from app.ai.context.session_events import FILE_UPLOADED
        await SessionEventService.emit_safe(
            db, report=SimpleNamespace(id=report_id), kind=FILE_UPLOADED,
            user=current_user,
            meta={"filename": file.filename, "file_id": str(result.id),
                  "content_type": file.content_type},
            target_type="file", target_id=str(result.id),
        )
    return result

@router.post("/data_sources/{data_source_id}/files", response_model=FileSchema)
@requires_resource_permission('data_source', 'manage')
async def upload_data_source_file(
    request: Request,
    data_source_id: str,
    file: UploadFile = File(...),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    result = await file_service.upload_file(db, file, current_user, organization, None, data_source_id)
    try:
        await audit_service.log(
            db=db,
            organization_id=organization.id,
            action="file.uploaded",
            user_id=current_user.id,
            resource_type="file",
            resource_id=result.id,
            details={"filename": file.filename, "content_type": file.content_type, "data_source_id": data_source_id},
            request=request,
        )
    except Exception:
        pass
    return result

@router.get("/data_sources/{data_source_id}/files", response_model=list[FileSchema])
@requires_resource_permission('data_source', 'view')
async def get_files_by_data_source(
    data_source_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await file_service.get_files_by_data_source(db, data_source_id, organization)

@router.delete("/data_sources/{data_source_id}/files/{file_id}")
@requires_resource_permission('data_source', 'manage')
async def remove_file_from_data_source(
    file_id: str,
    data_source_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await file_service.remove_file_from_data_source(db, file_id, data_source_id, organization, current_user)

@router.get("/reports/{report_id}/files", response_model=list[FileSchemaWithCompletionId])
@requires_permission('manage_files', model=Report)
async def get_files_by_report(report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await file_service.get_files_by_report(db, report_id, organization)

@router.delete("/reports/{report_id}/files/{file_id}")
@requires_permission('manage_files', model=Report)
async def remove_file_from_report(file_id: str, report_id: str, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    # Capture the filename before removal for the event text.
    _f = await db.get(FileModel, file_id)
    _fname = getattr(_f, "filename", None) if _f is not None else None
    result = await file_service.remove_file_from_report(db, file_id, report_id, organization, current_user)
    from types import SimpleNamespace
    from app.services.session_event_service import SessionEventService
    from app.ai.context.session_events import FILE_REMOVED
    await SessionEventService.emit_safe(
        db, report=SimpleNamespace(id=report_id), kind=FILE_REMOVED,
        user=current_user,
        meta={"filename": _fname, "file_id": str(file_id)},
        target_type="file", target_id=str(file_id),
    )
    return result

@router.get("/files", response_model=list[FileSchemaWithMetadata])
@requires_permission('manage_files')
async def get_files(current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    return await file_service.get_files(db, organization)

@router.get("/files/{file_id}/content")
@requires_permission('manage_files')
async def get_file_content(file_id: str, request: Request, current_user: User = Depends(current_user), db: AsyncSession = Depends(get_async_db), organization: Organization = Depends(get_current_organization)):
    """Serve file content (for displaying images in chat)."""
    # file_id must be a UUID — reject anything else at the entry so the
    # parameter cannot smuggle path characters further down (Snyk python/PT).
    import uuid as _uuid
    try:
        _uuid.UUID(file_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="File not found")

    stmt = select(FileModel).filter(FileModel.id == file_id, FileModel.organization_id == organization.id)
    result = await db.execute(stmt)
    file = result.scalar_one_or_none()

    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    if not file.path:
        raise HTTPException(status_code=404, detail="File content not found")

    # Path-traversal guard at the sink. Uploaded files (and tool outputs) are
    # always stored flat under uploads/files/, so rebuild the path to open from
    # the trusted root plus the sanitized basename. os.path.basename strips any
    # directory-traversal sequences, so a tampered DB value can never make
    # open() escape uploads/files/.
    safe_path = os.path.join(os.getcwd(), "uploads", "files", os.path.basename(file.path))
    if not os.path.exists(safe_path):
        raise HTTPException(status_code=404, detail="File content not found")

    try:
        await audit_service.log(
            db=db,
            organization_id=organization.id,
            action="file.downloaded",
            user_id=current_user.id,
            resource_type="file",
            resource_id=file_id,
            details={"filename": file.filename},
            request=request,
        )
    except Exception:
        pass

    # safe_path is verified above to live under uploads/ — read its bytes and
    # serve as an in-memory response so no path string is handed to a
    # framework file API.
    with open(safe_path, "rb") as _fh:
        content = _fh.read()
    return Response(
        content=content,
        media_type=file.content_type,
        headers={
            "Content-Disposition": f'attachment; filename="{file.filename}"',
        },
    )


def _read_file_bytes_or_404(file: FileModel) -> bytes:
    """Read a stored file's bytes from the trusted uploads root (path-traversal
    guarded), or raise 404. Shared by the authed and token-gated servers."""
    if not file.path:
        raise HTTPException(status_code=404, detail="File content not found")
    safe_path = os.path.join(os.getcwd(), "uploads", "files", os.path.basename(file.path))
    if not os.path.exists(safe_path):
        raise HTTPException(status_code=404, detail="File content not found")
    with open(safe_path, "rb") as _fh:
        return _fh.read()


@router.get("/files/{file_id}/embed_token")
@requires_permission('manage_files')
async def get_file_embed_token(
    file_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Mint a short-lived, file-scoped capability token for embedding.

    The token lets an artifact sandbox iframe (which can't send an auth header)
    load this file via GET /files/{id}/embed?token=… . Authorized by org
    membership here; the token is never persisted (minted fresh per render)."""
    import uuid as _uuid
    try:
        _uuid.UUID(file_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="File not found")

    file = (await db.execute(
        select(FileModel).filter(FileModel.id == file_id, FileModel.organization_id == organization.id)
    )).scalar_one_or_none()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    from app.core.file_tokens import mint_file_token, file_embed_url
    token = mint_file_token(file_id)
    return {"file_id": file_id, "token": token, "url": file_embed_url(file_id, token)}


@router.get("/files/{file_id}/embed")
async def get_file_embed(
    file_id: str,
    token: str,
    db: AsyncSession = Depends(get_async_db),
):
    """Serve a file's bytes when presented a valid capability token — no session.

    Used by artifact sandboxes and published-report pages. The token is a
    capability for exactly this file id; org membership is NOT required because
    access was already authorized when the token was minted."""
    import uuid as _uuid
    try:
        _uuid.UUID(file_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="File not found")

    from app.core.file_tokens import verify_file_token
    if not verify_file_token(token, file_id):
        raise HTTPException(status_code=403, detail="Invalid or expired file token")

    # Load by id only — the token is the capability, not the session.
    file = (await db.execute(
        select(FileModel).filter(FileModel.id == file_id)
    )).scalar_one_or_none()
    if not file:
        raise HTTPException(status_code=404, detail="File not found")

    content = _read_file_bytes_or_404(file)
    return Response(
        content=content,
        media_type=file.content_type or "application/octet-stream",
        headers={
            # inline so <img>/<iframe> render it rather than downloading
            "Content-Disposition": f'inline; filename="{file.filename or file_id}"',
            "Cache-Control": "private, max-age=300",
        },
    )