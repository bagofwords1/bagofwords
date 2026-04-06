"""
Central registry of all permission strings used in the application.

This is the single source of truth for valid permissions. Route decorators
reference these strings, the frontend receives them via whoami, and the
RolesManager UI groups them by category for the role editor.

MVP scope: 15 org-level permissions + 9 data_source resource grants, plus
the `full_admin_access` wildcard. Reports/builds/widgets/entities are
derived from data_source access. Connection and report resource grants
land post-MVP — the resource_grants table is generic, no schema change
needed.
"""

# ── Org-level Permission Categories ──────────────────────────────────────
# Used by the frontend RolesManager to group checkboxes.

PERMISSION_CATEGORIES = {
    "Reports": [
        "view_reports",
        "create_reports",
        "update_reports",
        "delete_reports",
        "publish_reports",
    ],
    "Files": [
        "manage_files",
    ],
    "Data & Connections": [
        "create_data_source",
        "manage_connections",
    ],
    "Instructions": [
        "manage_instructions",
    ],
    "Evals": [
        "manage_evals",
    ],
    "Members": [
        "view_members",
        "manage_members",
    ],
    "Settings": [
        "manage_settings",
        "manage_llm",
    ],
    "Enterprise": [
        "view_audit_logs",
        "manage_scim",
        "manage_ldap",
    ],
}

# Flatten to get all valid permission strings (excludes the full_admin_access wildcard)
ALL_PERMISSIONS = set()
for perms in PERMISSION_CATEGORIES.values():
    ALL_PERMISSIONS.update(perms)

# ── Resource Permission Options ──────────────────────────────────────────
# Available permission strings for resource_grants by resource type.
# MVP: data_source only. connection/report grants are post-MVP.

RESOURCE_PERMISSIONS = {
    "data_source": [
        "view",
        "view_schema",
        "view_instructions",
        "create_instructions",
        "view_entities",
        "create_entities",
        "manage_evals",
        "manage",
        "manage_members",
    ],
}

# ── Merged categories for the role editor UI ─────────────────────────────
# Groups related categories into fewer rows for a cleaner modal.

MERGED_CATEGORIES = {
    "Reports & Files": ["Reports", "Files"],
    "Data & Instructions": ["Data & Connections", "Instructions", "Evals"],
    "Members & Access": ["Members"],
    "Settings & Admin": ["Settings", "Enterprise"],
}

# Resource-scoped permission groups — shown per-resource in the role editor.

RESOURCE_SCOPED_GROUPS = {
    "data_source": {
        "Access": ["view", "view_schema"],
        "Instructions": ["view_instructions", "create_instructions"],
        "Entities": ["view_entities", "create_entities"],
        "Evals": ["manage_evals"],
        "Management": ["manage", "manage_members"],
    },
}


# ── Default Role Permission Sets ─────────────────────────────────────────
# These define what the system-seeded admin and member roles contain.

# DS Member: can work with reports and files; sees members list. No DS/instruction/eval admin.
DEFAULT_MEMBER_PERMISSIONS = [
    "view_reports",
    "create_reports",
    "update_reports",
    "delete_reports",
    "publish_reports",
    "manage_files",
    "view_members",
]

# DS Admin: gets all 15 org perms. Owner role uses full_admin_access wildcard separately.
DEFAULT_ADMIN_PERMISSIONS = ["full_admin_access"]


# ── Migration Translation Table ──────────────────────────────────────────
# Maps every legacy permission string to its MVP replacement.
# `None` means: drop the permission entirely (call sites become unguarded
# or fall through to a different perm — handled per-route in Phase 3).
#
# Used by:
#   - Phase 3 route remapping (canonical lookup)
#   - Phase 7 Alembic migration (translate existing custom roles)
#
# Removed after the migration window (Phase 9 cleanup).

MVP_OLD_TO_NEW_PERM_MAP: dict[str, str | None] = {
    # Reports — mostly unchanged
    "view_reports": "view_reports",
    "create_reports": "create_reports",
    "update_reports": "update_reports",
    "delete_reports": "delete_reports",
    "publish_reports": "publish_reports",
    "rerun_report_steps": "update_reports",  # collapsed into update

    # Data sources — collapsed into create + resource grants
    "view_data_source": None,  # derived from data_source resource grant `view`
    "create_data_source": "create_data_source",
    "update_data_source": None,  # via data_source resource grant `manage`
    "delete_data_source": None,  # via data_source resource grant `manage`
    "view_data_source_full_schema": None,  # via data_source resource grant `view_schema`
    "manage_data_source_memberships": None,  # via data_source resource grant `manage_members`

    # Connections
    "manage_connections": "manage_connections",
    "view_connections": "manage_connections",  # MVP collapses view→manage at org level

    # Queries — dropped entirely
    "export_query": None,

    # Files — collapsed
    "view_files": "manage_files",
    "upload_files": "manage_files",
    "delete_files": "manage_files",

    # Members & RBAC — collapsed into view/manage_members
    "view_organization_members": "view_members",
    "add_organization_members": "manage_members",
    "update_organization_members": "manage_members",
    "remove_organization_members": "manage_members",
    "manage_roles": "manage_members",
    "manage_groups": "manage_members",
    "manage_role_assignments": "manage_members",
    "manage_resource_grants": "manage_members",

    # Instructions — collapsed into manage_instructions org perm
    # (per-DS create still possible via data_source `create_instructions` grant)
    "view_instructions": "manage_instructions",
    "create_instructions": "manage_instructions",
    "update_instructions": "manage_instructions",
    "delete_instructions": "manage_instructions",
    "view_global_instructions": None,
    "view_hidden_instructions": None,
    "suggest_instructions": None,

    # Entities — derived from data_source grants
    "view_entities": None,
    "create_entities": None,
    "update_entities": None,
    "delete_entities": None,
    "refresh_entities": None,
    "approve_entities": None,
    "reject_entities": None,
    "suggest_entities": None,
    "withdraw_entities": None,

    # Evals — collapsed
    "manage_evals": "manage_evals",
    "run_evals": "manage_evals",
    "view_evals": "manage_evals",

    # Builds — derived from manage_instructions
    "view_builds": "manage_instructions",
    "create_builds": "manage_instructions",

    # Settings — collapsed
    "view_settings": "manage_settings",
    "modify_settings": "manage_settings",
    "view_organizations": None,
    "manage_organization_settings": "manage_settings",
    "view_organization_settings": "manage_settings",
    "view_organization_overview": None,
    "manage_organization_external_platforms": "manage_settings",
    "manage_llm_settings": "manage_llm",
    "view_llm_settings": "manage_llm",
    "train_mode": None,

    # Feedback — dropped
    "create_completion_feedback": None,
    "view_all_completion_feedbacks": None,

    # Enterprise — kept
    "view_audit_logs": "view_audit_logs",
    "manage_scim": "manage_scim",
    "manage_ldap": "manage_ldap",
}
