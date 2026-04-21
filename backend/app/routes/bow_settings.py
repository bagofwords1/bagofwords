import os

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from app.settings.config import settings
from app.dependencies import get_current_locale, get_current_organization, _locale_from_org
from app.models.organization import Organization

router = APIRouter()

@router.get("/settings", tags=["settings"])
async def get_frontend_settings():
    """Get frontend configuration settings"""
    is_testing = os.getenv("TESTING", "").lower() == "true"
    
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
            "enabled": settings.bow_config.intercom.enabled and not is_testing,
        },
        "telemetry": {
            "enabled": settings.bow_config.telemetry.enabled and not is_testing,
        },
        "smtp_enabled": settings.bow_config.smtp_settings is not None,
        "version": settings.PROJECT_VERSION,
        "environment": settings.ENVIRONMENT,
        "i18n": {
            "default_locale": settings.bow_config.i18n.default_locale,
            "enabled_locales": settings.bow_config.i18n.enabled_locales,
            "fallback_locale": settings.bow_config.i18n.fallback_locale,
        },
    })


@router.get("/config/i18n", tags=["settings"])
async def get_i18n_config(request: Request):
    """Public i18n config: available locales and effective locale for this request.

    When an org header is present and valid, returns the org-overridden locale;
    otherwise returns the system default. X-Locale header (if in enabled list)
    takes highest priority.
    """
    i18n = settings.bow_config.i18n
    current_locale = await get_current_locale(request)

    org_locale = None
    org_id = request.headers.get("X-Organization-Id")
    if org_id:
        try:
            from app.dependencies import get_async_session
            from sqlalchemy import select
            async for db in get_async_session():
                org = (await db.execute(select(Organization).filter(Organization.id == org_id))).scalar_one_or_none()
                if org is not None:
                    org_locale = _locale_from_org(org)
                break
        except Exception:
            org_locale = None

    override = request.headers.get("X-Locale")
    if override and override in i18n.enabled_locales:
        current_locale = override
    elif org_locale:
        current_locale = org_locale

    return JSONResponse({
        "default_locale": i18n.default_locale,
        "enabled_locales": i18n.enabled_locales,
        "fallback_locale": i18n.fallback_locale,
        "current_locale": current_locale,
        "org_locale": org_locale,
    })
