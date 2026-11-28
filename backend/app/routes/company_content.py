"""Guard endpoints that return a historical report payload or a derived export."""
from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_async_db, get_current_organization
from app.models.organization import Organization
from app.services.viewer_data_policy import org_has_company_policy


async def require_stored_company_content(
    organization: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_async_db),
):
    if await org_has_company_policy(db, organization.id):
        raise HTTPException(status_code=404, detail="Stored report unavailable")
