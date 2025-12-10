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
        "auth": {
            "mode": getattr(settings.bow_config, 'auth').mode if hasattr(settings.bow_config, 'auth') else 'hybrid'
        },
        "oidc_providers": [
            {
                "name": p.name,
                "enabled": p.enabled
            } for p in getattr(settings.bow_config, "oidc_providers", []) or []
        ],
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
        "smtp_enabled": settings.bow_config.smtp_settings is not None,
        "version": settings.PROJECT_VERSION,
        "environment": settings.ENVIRONMENT,
    })
