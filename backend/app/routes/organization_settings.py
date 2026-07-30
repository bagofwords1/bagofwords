from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.core.auth import current_user
from app.core.permissions_decorator import requires_permission
from app.services.organization_settings_service import OrganizationSettingsService
from app.schemas.organization_settings_schema import (
    EntraProfileSyncConfig,
    OrgSmtpSchema,
    OrgSmtpUpdate,
    OrganizationSettingsSchema,
    OrganizationSettingsUpdate,
    SignupPolicySchema,
)

router = APIRouter(tags=["organization_settings"])
settings_service = OrganizationSettingsService()

@router.get("/organization/settings", response_model=OrganizationSettingsSchema)
async def get_organization_settings(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Get all settings for the organization"""
    return await settings_service.get_settings(db, organization, current_user)

@router.put("/organization/settings", response_model=OrganizationSettingsSchema)
@requires_permission('manage_settings')
async def update_organization_settings(
    settings: OrganizationSettingsUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Update organization settings"""
    return await settings_service.update_settings(db, organization, current_user, settings)

@router.post("/organization/settings/agents/{agent_name}")
@requires_permission('manage_settings')
async def update_agent_setting(
    agent_name: str,
    enabled: bool,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    """Enable/disable a specific agent"""
    return await settings_service.update_agent_setting(db, organization, current_user, agent_name, enabled) 


@router.post("/organization/general/icon", response_model=OrganizationSettingsSchema)
@requires_permission('manage_settings')
async def upload_general_icon(
    icon: UploadFile = File(...),
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await settings_service.set_general_icon(db, organization, current_user, icon)


@router.delete("/organization/general/icon", response_model=OrganizationSettingsSchema)
@requires_permission('manage_settings')
async def delete_general_icon(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization)
):
    return await settings_service.remove_general_icon(db, organization, current_user)


@router.get("/organization/locale")
async def get_organization_locale(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Effective locale for this org + enabled list + system default."""
    return await settings_service.get_locale(db, organization, current_user)


@router.put("/organization/locale")
@requires_permission('manage_settings')
async def update_organization_locale(
    payload: dict,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Set the org's locale override. Pass {"locale": "en|es|he"} or {"locale": null} to clear."""
    locale = payload.get("locale")
    return await settings_service.update_locale(db, organization, current_user, locale)


@router.get("/organization/timezone")
async def get_organization_timezone(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Org timezone override + effective tz (UTC fallback)."""
    return await settings_service.get_timezone(db, organization, current_user)


@router.put("/organization/timezone")
@requires_permission('manage_settings')
async def update_organization_timezone(
    payload: dict,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Set the org timezone. Pass {"timezone": "America/New_York"} or {"timezone": null} to clear."""
    timezone = payload.get("timezone")
    return await settings_service.update_timezone(db, organization, current_user, timezone)


@router.get("/organization/timezones")
async def list_supported_timezones(
    current_user: User = Depends(current_user),
):
    """Sorted IANA timezone names for the settings picker."""
    from zoneinfo import available_timezones
    return {"timezones": sorted(available_timezones())}


@router.get("/organization/week_start")
async def get_organization_week_start(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Org first-day-of-week override + the effective value (derived from locale)."""
    return await settings_service.get_week_start(db, organization, current_user)


@router.put("/organization/week_start")
@requires_permission('manage_settings')
async def update_organization_week_start(
    payload: dict,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Set the org first day of week. Pass {"week_start": "sunday"} or {"week_start": null} to auto-derive."""
    week_start = payload.get("week_start")
    return await settings_service.update_week_start(db, organization, current_user, week_start)


@router.get("/organization/signup-policy", response_model=SignupPolicySchema)
@requires_permission('full_admin_access')
async def get_signup_policy(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await settings_service.get_signup_policy(db, organization, current_user)


@router.put("/organization/signup-policy", response_model=SignupPolicySchema)
@requires_permission('full_admin_access')
async def update_signup_policy(
    policy: SignupPolicySchema,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await settings_service.update_signup_policy(db, organization, current_user, policy)


# --- Entra ID profile / job-info sync (identity providers page) -----------

@router.get("/organization/identity/entra-profile-sync", response_model=EntraProfileSyncConfig)
@requires_permission('manage_identity_providers')
async def get_entra_profile_sync(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Return the org's Entra profile-sync setting (disabled by default)."""
    return await settings_service.get_entra_profile_sync(db, organization, current_user)


@router.put("/organization/identity/entra-profile-sync", response_model=EntraProfileSyncConfig)
@requires_permission('manage_identity_providers')
async def update_entra_profile_sync(
    payload: EntraProfileSyncConfig,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Enable/disable Entra profile sync and choose which Graph /me fields to store."""
    return await settings_service.update_entra_profile_sync(db, organization, current_user, payload)


@router.get("/organization/identity/entra-profile-sync/preview")
@requires_permission('manage_identity_providers')
async def preview_entra_profile_sync(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Sample values from the admin's own Graph /me, so they can see what each
    attribute contains before choosing which to sync into AI context."""
    return await settings_service.preview_entra_profile(db, organization, current_user)


@router.get("/organization/smtp", response_model=OrgSmtpSchema)
@requires_permission('manage_settings')
async def get_org_smtp(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await settings_service.get_smtp(db, organization, current_user)


@router.put("/organization/smtp", response_model=OrgSmtpSchema)
@requires_permission('manage_settings')
async def update_org_smtp(
    data: OrgSmtpUpdate,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await settings_service.update_smtp(db, organization, current_user, data)


@router.post("/organization/smtp/test", response_model=dict)
@requires_permission('manage_settings')
async def test_org_smtp(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    return await settings_service.test_smtp(db, organization, current_user)


# --- PII protection (enterprise) ------------------------------------------

@router.get("/organization/pii/builtin-rules", response_model=dict)
@requires_permission('manage_settings')
async def get_pii_builtin_rules(
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """The built-in PII rule catalogue (code-defined) the settings page renders
    so admins can enable/disable each and override its replacement token."""
    from app.ee.license import has_feature
    from app.ai.llm.pii.builtin_rules import BUILTIN_PII_RULES
    return {
        "licensed": has_feature("pii_protection"),
        "rules": [
            {
                "id": r["id"],
                "name": r["name"],
                "replacement": r["replacement"],
                "pattern_count": len(r["patterns"]),
            }
            for r in BUILTIN_PII_RULES
        ],
    }


@router.post("/organization/pii/test", response_model=dict)
@requires_permission('manage_settings')
async def test_pii_redaction(
    payload: dict,
    current_user: User = Depends(current_user),
    db: AsyncSession = Depends(get_async_db),
    organization: Organization = Depends(get_current_organization),
):
    """Dry-run the org's PII rules against sample text so admins can preview
    redaction from the settings page. Enterprise-gated. Accepts an optional
    ``config`` override to preview unsaved changes; otherwise uses stored config.

    Returns the redacted text and a non-sensitive per-rule match summary.
    """
    from app.ee.license import has_feature
    if not has_feature("pii_protection"):
        raise HTTPException(status_code=402, detail="PII protection requires an enterprise license.")

    from app.ai.llm.pii.redactor import build_redactor, validate_pattern, PiiPromptBlockedError

    text = payload.get("text") or ""
    pii_config = payload.get("config")

    if not isinstance(pii_config, dict):
        return {"text": text, "matches": [], "blocked": False, "mode": "replace"}

    # Validate any inline custom patterns before compiling so the preview
    # surfaces regex errors just like a save would.
    for rule in pii_config.get("custom_rules") or []:
        for pat in (rule.get("patterns") or []):
            err = validate_pattern(pat)
            if err:
                raise HTTPException(status_code=400, detail=f"Rule '{rule.get('name', '')}': {err}")

    # Preview mode always treats the feature as enabled for the test.
    redactor = build_redactor({**pii_config, "enabled": True})
    if redactor is None:
        return {"text": text, "matches": [], "blocked": False, "mode": "replace"}

    try:
        redacted, result = redactor.apply(text)
        return {
            "text": redacted,
            "matches": result.matches,
            "blocked": False,
            "mode": redactor.mode,
        }
    except PiiPromptBlockedError as e:
        return {"text": text, "matches": [{"name": n} for n in e.rule_names], "blocked": True, "mode": "block"}