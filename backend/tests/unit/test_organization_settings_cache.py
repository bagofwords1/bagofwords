from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings


@pytest.mark.asyncio
async def test_get_settings_reuses_joined_relationship() -> None:
    organization = Organization(id=str(uuid4()), name="Settings cache test")
    settings = OrganizationSettings(organization_id=organization.id)
    organization.settings = settings
    db = AsyncMock()

    result = await organization.get_settings(db)

    assert result is settings
    db.execute.assert_not_awaited()
