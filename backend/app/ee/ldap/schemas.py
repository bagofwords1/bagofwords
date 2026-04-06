# LDAP Schemas
# Licensed under the Business Source License 1.1
# See ENTERPRISE_LICENSE for details

from typing import Optional, List
from datetime import datetime
from pydantic import BaseModel


class LDAPConfigUpdate(BaseModel):
    """Request body for saving LDAP config per org (future: DB-backed)."""
    url: str
    bind_dn: Optional[str] = None
    bind_password: Optional[str] = None
    use_ssl: bool = True
    start_tls: bool = False
    base_dn: str
    user_search_base: Optional[str] = None
    user_search_filter: str = "(objectClass=person)"
    user_email_attribute: str = "mail"
    user_name_attribute: str = "displayName"
    group_search_base: Optional[str] = None
    group_search_filter: str = "(objectClass=group)"
    group_name_attribute: str = "cn"
    group_member_attribute: str = "member"
    group_member_format: str = "dn"
    sync_interval_minutes: int = 60


class SyncResult(BaseModel):
    """Result of an LDAP group sync operation."""
    groups_created: int = 0
    groups_updated: int = 0
    groups_removed: int = 0
    memberships_added: int = 0
    memberships_removed: int = 0
    users_not_found: int = 0
    errors: List[str] = []
    timestamp: Optional[datetime] = None


class SyncStatus(BaseModel):
    """Current LDAP sync status for an organization."""
    last_sync: Optional[SyncResult] = None
    is_syncing: bool = False
    ldap_configured: bool = False


class LDAPGroupPreview(BaseModel):
    """Preview of a single LDAP group."""
    dn: str
    name: str
    member_count: int
    exists_in_app: bool = False
    members_to_add: int = 0
    members_to_remove: int = 0


class LDAPSyncPreview(BaseModel):
    """Dry-run preview of what a sync would change."""
    groups_to_create: int = 0
    groups_to_update: int = 0
    groups_to_remove: int = 0
    total_membership_changes: int = 0
    groups: List[LDAPGroupPreview] = []


class LDAPTestResult(BaseModel):
    """Result of testing LDAP connection."""
    connected: bool
    server: str
    vendor: Optional[str] = None
    error: Optional[str] = None
    user_count: Optional[int] = None
    group_count: Optional[int] = None
