# SCIM Authentication
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

import hashlib
import logging
from datetime import datetime

from fastapi import Request, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.dependencies import get_async_db
from app.ee.license import has_feature
from app.ee.scim.models import ScimToken
from app.models.organization import Organization

logger = logging.getLogger(__name__)


async def scim_auth(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
) -> Organization:
    """
    FastAPI dependency that authenticates SCIM requests via Bearer token.

    Returns the Organization associated with the token.
    Raises 401 if token is invalid, 402 if SCIM feature is not licensed.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = auth_header[7:]  # Strip "Bearer "
    if not token.startswith("bow_scim_"):
        raise HTTPException(
            status_code=401,
            detail="Invalid SCIM token format",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Hash and look up
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    result = await db.execute(
        select(ScimToken)
        .where(ScimToken.token_hash == token_hash)
        .where(ScimToken.deleted_at.is_(None))
    )
    scim_token = result.scalar_one_or_none()

    if not scim_token:
        raise HTTPException(
            status_code=401,
            detail="Invalid SCIM token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check expiration
    if scim_token.expires_at and scim_token.expires_at < datetime.utcnow():
        raise HTTPException(
            status_code=401,
            detail="SCIM token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Check enterprise license
    if not has_feature("scim"):
        raise HTTPException(
            status_code=402,
            detail="SCIM provisioning requires an enterprise license.",
        )

    # Load organization
    org_result = await db.execute(
        select(Organization).where(Organization.id == scim_token.organization_id)
    )
    organization = org_result.scalar_one_or_none()

    if not organization:
        raise HTTPException(status_code=401, detail="Organization not found")

    # Update last_used_at
    scim_token.last_used_at = datetime.utcnow()
    await db.commit()

    return organization
