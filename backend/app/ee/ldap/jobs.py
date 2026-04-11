# LDAP Background Sync Jobs
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

import logging

from app.dependencies import async_session_maker
from app.settings.config import settings
from app.ee.ldap.sync_service import LDAPGroupSyncService
from app.models.organization import Organization
from sqlalchemy import select

logger = logging.getLogger(__name__)


async def ldap_sync_all_organizations():
    """Background job: sync LDAP groups for all organizations."""
    ldap_config = settings.bow_config.ldap
    if not ldap_config.enabled:
        return

    sync_service = LDAPGroupSyncService(ldap_config)

    async with async_session_maker() as db:
        orgs = (await db.execute(select(Organization))).scalars().all()
        for org in orgs:
            try:
                result = await sync_service.sync_groups(db, str(org.id))
                if result.errors:
                    logger.warning(f"LDAP sync for org {org.id} completed with errors: {result.errors}")
            except Exception as e:
                logger.error(f"LDAP sync failed for org {org.id}: {e}")
