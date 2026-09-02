# Enterprise features for Bag of Words
# Licensed under the Bag of Words Enterprise License
# See backend/app/ee/LICENSE for details

from app.ee.license import (
    is_enterprise_licensed,
    get_license_info,
    require_enterprise,
    is_datasource_allowed,
    get_max_users,
    get_max_agents,
    LicenseInfo,
    ENTERPRISE_DATASOURCES,
)

__all__ = [
    "is_enterprise_licensed",
    "get_license_info",
    "require_enterprise",
    "is_datasource_allowed",
    "get_max_users",
    "get_max_agents",
    "LicenseInfo",
    "ENTERPRISE_DATASOURCES",
]
