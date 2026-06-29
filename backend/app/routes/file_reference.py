"""Routes for durable connector file references (A3) + a picker listing endpoint."""
from fastapi import APIRouter, Depends, HTTPException, Body, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional, List

from app.dependencies import get_async_db, get_current_organization
from app.core.auth import current_user
from app.models.user import User
from app.models.organization import Organization
from app.models.report import Report
from app.models.connection import Connection
from app.models.file_reference import FileReference

router = APIRouter(tags=["file_references"])


def _ref_dict(r: FileReference) -> dict:
    return {
        "id": r.id, "report_id": r.report_id, "connection_id": r.connection_id,
        "external_file_id": r.external_file_id, "name": r.name, "mime": r.mime,
    }


@router.get("/reports/{report_id}/file_references", response_model=List[dict])
async def list_file_references(
    report_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    rows = (await db.execute(
        select(FileReference).where(
            FileReference.report_id == report_id,
            FileReference.organization_id == str(organization.id),
        )
    )).scalars().all()
    return [_ref_dict(r) for r in rows]


@router.post("/reports/{report_id}/file_references", response_model=dict)
async def create_file_reference(
    report_id: str,
    connection_id: str = Body(...),
    external_file_id: str = Body(...),
    name: Optional[str] = Body(None),
    mime: Optional[str] = Body(None),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    report = await db.get(Report, report_id)
    if not report or str(report.organization_id) != str(organization.id):
        raise HTTPException(status_code=404, detail="Report not found")
    conn = await db.get(Connection, connection_id)
    if not conn or str(conn.organization_id) != str(organization.id):
        raise HTTPException(status_code=404, detail="Connection not found")
    ref = FileReference(
        report_id=report_id, connection_id=connection_id,
        external_file_id=external_file_id, name=name, mime=mime,
        organization_id=str(organization.id), created_by_user_id=str(current_user.id),
    )
    db.add(ref)
    await db.commit()
    await db.refresh(ref)
    return _ref_dict(ref)


@router.delete("/file_references/{reference_id}")
async def delete_file_reference(
    reference_id: str,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    ref = await db.get(FileReference, reference_id)
    if not ref or str(ref.organization_id) != str(organization.id):
        raise HTTPException(status_code=404, detail="Reference not found")
    await db.delete(ref)
    await db.commit()
    return {"success": True}


@router.get("/connections/{connection_id}/files", response_model=List[dict])
async def list_connection_files(
    connection_id: str,
    search: Optional[str] = Query(None, description="Search query; omit to list recent files"),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """List/search files exposed by a file-source connection, for the picker.
    Resolves under the current user's credentials (per-user)."""
    conn = await db.get(Connection, connection_id)
    if not conn or str(conn.organization_id) != str(organization.id):
        raise HTTPException(status_code=404, detail="Connection not found")
    from app.services.connection_service import ConnectionService
    try:
        client = await ConnectionService().construct_client(db, conn, current_user)
        files = await (client.asearch_files(search) if search else client.alist_files())
    except NotImplementedError:
        raise HTTPException(status_code=400, detail="This connection does not support file listing.")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to list files: {e}")
    out = []
    for f in (files or [])[:50]:
        out.append({"id": f.get("id"), "name": f.get("name"), "mime": f.get("mimeType")})
    return out
