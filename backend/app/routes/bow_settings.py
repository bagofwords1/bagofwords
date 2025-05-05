from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.settings.config import settings

router = APIRouter()

@router.get("/settings", tags=["settings"])
async def get_frontend_settings():
    """Get frontend configuration settings"""
    return JSONResponse({
        "google_oauth": {
            "enabled": settings.bow_config.google_oauth.enabled,
        },
        "features": {
            "allow_uninvited_signups": settings.bow_config.features.allow_uninvited_signups,
            "allow_multiple_organizations": settings.bow_config.features.allow_multiple_organizations,
            "verify_emails": settings.bow_config.features.verify_emails,
        },
        "deployment": {
            "type": settings.bow_config.deployment.type if hasattr(settings.bow_config, 'deployment') else "development",
        },
        "base_url": settings.bow_config.base_url,
        "intercom": {
            "enabled": settings.bow_config.intercom.enabled,
        },
        "version": settings.PROJECT_VERSION,
        "environment": settings.ENVIRONMENT,
    })
