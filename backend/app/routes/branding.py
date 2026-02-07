from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
import os

router = APIRouter(tags=["settings"])

@router.get("/general/icon/{icon_key}")
async def get_general_icon(icon_key: str):
    base_dir = os.path.abspath(os.path.join(os.getcwd(), "uploads", "branding"))
    path = os.path.join(base_dir, icon_key)
    if not os.path.exists(path):
        # Let FastAPI raise 404 automatically by not returning a file
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Icon not found")
    # Basic caching headers could be added via Response, but simple FileResponse is okay now
    return FileResponse(path)


@router.get("/thumbnails/{filename}")
async def get_thumbnail(filename: str):
    """Serve thumbnail images for artifacts/reports."""
    base_dir = os.path.abspath(os.path.join(os.getcwd(), "uploads", "thumbnails"))
    path = os.path.join(base_dir, filename)
    if not os.path.exists(path):
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Thumbnail not found")
    return FileResponse(path, media_type="image/png")


