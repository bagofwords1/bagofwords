"""Organization-owned resource availability, read fresh at each access boundary."""
from sqlalchemy import select
from app.models.organization_settings import OrganizationSettings


async def artifact_resources_enabled(db, organization_id):
    config = await db.scalar(select(OrganizationSettings.config).where(
        OrganizationSettings.organization_id == str(organization_id)))
    value = (config or {}).get('enable_artifact_resources', True)
    if isinstance(value, dict):
        if value.get('state') == 'locked':
            return False
        value = value.get('value', value.get('state') == 'enabled')
    return value is True


async def require_artifact_resources(db, organization_id):
    from app.services.artifact_resource_service import fail
    if not await artifact_resources_enabled(db, organization_id):
        fail('UNAVAILABLE', 'Artifact resources are disabled in organization settings. An organization admin can enable Artifact resources in AI settings.', 404)
