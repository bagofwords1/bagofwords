import os
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from app.dependencies import get_async_db
from app.services import storage

router = APIRouter(tags=["settings"])

# Single-segment basename: no path separators, no traversal.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,200}$")


@router.get("/general/icon/{icon_key}")
async def get_general_icon(icon_key: str):
    # Inline path-traversal guard at the sink (Snyk python/PT).
    if ".." in icon_key or "/" in icon_key or "\\" in icon_key:
        raise HTTPException(status_code=404, detail="Icon not found")
    if not _SAFE_NAME_RE.fullmatch(icon_key):
        raise HTTPException(status_code=404, detail="Icon not found")

    base_dir = os.path.realpath(os.path.join(os.getcwd(), "uploads", "branding"))
    resolved = os.path.realpath(os.path.join(base_dir, icon_key))
    if not resolved.startswith(base_dir + os.sep):
        raise HTTPException(status_code=404, detail="Icon not found")
    if not await storage.aensure_local(resolved):
        raise HTTPException(status_code=404, detail="Icon not found")
    return FileResponse(resolved)


@router.get("/users/avatar/{avatar_key}")
async def get_user_avatar(avatar_key: str):
    """Serve an uploaded user avatar. Public (like the org icon): avatars are
    non-sensitive and need to render in <img> tags without auth headers."""
    if ".." in avatar_key or "/" in avatar_key or "\\" in avatar_key:
        raise HTTPException(status_code=404, detail="Avatar not found")
    if not _SAFE_NAME_RE.fullmatch(avatar_key):
        raise HTTPException(status_code=404, detail="Avatar not found")

    base_dir = os.path.realpath(os.path.join(os.getcwd(), "uploads", "avatars"))
    resolved = os.path.realpath(os.path.join(base_dir, avatar_key))
    if not resolved.startswith(base_dir + os.sep):
        raise HTTPException(status_code=404, detail="Avatar not found")
    if not await storage.aensure_local(resolved):
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(resolved, media_type="image/png")


@router.get("/thumbnails/{filename}")
async def get_thumbnail(filename: str, db=Depends(get_async_db)):
    """Serve thumbnail images for artifacts/reports."""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    if not _SAFE_NAME_RE.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    base_dir = os.path.realpath(os.path.join(os.getcwd(), "uploads", "thumbnails"))
    resolved = os.path.realpath(os.path.join(base_dir, filename))
    if not resolved.startswith(base_dir + os.sep):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    from sqlalchemy import select
    from app.models.artifact import ArtifactVersion
    from app.models.artifact_release import ArtifactRelease
    from app.services.viewer_data_policy import org_has_company_policy

    relative_path = f"thumbnails/{filename}"
    owners = select(ArtifactVersion.organization_id).where(
        ArtifactVersion.thumbnail_path == relative_path, ArtifactVersion.deleted_at.is_(None)
    ).union(select(ArtifactRelease.organization_id).where(
        ArtifactRelease.thumbnail_path == relative_path, ArtifactRelease.deleted_at.is_(None)
    ))
    org_ids = (await db.scalars(owners)).all()
    if not org_ids or len(org_ids) != 1 or await org_has_company_policy(db, org_ids[0]):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    if not await storage.aensure_local(resolved):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(resolved, media_type="image/png", headers={"Cache-Control": "no-store"})
