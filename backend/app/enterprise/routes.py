# Enterprise Routes
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

from fastapi import APIRouter
from app.enterprise.license import get_license_info, LicenseInfo
from app.enterprise.audit.routes import router as audit_router

router = APIRouter(tags=["enterprise"])

# Include sub-routers
router.include_router(audit_router)


@router.get("/license", response_model=LicenseInfo)
async def get_license_status():
    """
    Get current license status.
    This endpoint is public and does not require authentication.
    """
    return get_license_info()
