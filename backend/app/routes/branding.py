import os
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

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
    if not os.path.isfile(resolved):
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
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(resolved, media_type="image/png")


@router.get("/thumbnails/{filename}")
async def get_thumbnail(filename: str):
    """Serve thumbnail images for artifacts/reports."""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    if not _SAFE_NAME_RE.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Thumbnail not found")

    base_dir = os.path.realpath(os.path.join(os.getcwd(), "uploads", "thumbnails"))
    resolved = os.path.realpath(os.path.join(base_dir, filename))
    if not resolved.startswith(base_dir + os.sep):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(resolved, media_type="image/png")
