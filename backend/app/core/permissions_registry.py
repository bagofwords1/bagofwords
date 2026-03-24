"""
Central registry of all permission strings used in the application.

This is the single source of truth for valid permissions. Route decorators
reference these strings, the frontend receives them via whoami, and the
RolesManager UI groups them by category for the role editor.

To add a new permission:
1. Add it to the appropriate category below
2. Add it to DEFAULT_MEMBER_PERMISSIONS or DEFAULT_ADMIN_PERMISSIONS if applicable
3. Use it in a @requires_permission() decorator on the relevant route
"""

# ── Permission Categories ────────────────────────────────────────────────
# Used by the frontend RolesManager to group checkboxes

PERMISSION_CATEGORIES = {
    "Reports": [
        "view_reports",
        "create_reports",
        "update_reports",
        "delete_reports",
        "publish_reports",
        "rerun_report_steps",
    ],
    "Data Sources": [
        "view_data_source",
        "create_data_source",
        "update_data_source",
        "delete_data_source",
        "view_data_source_full_schema",
        "manage_data_source_memberships",
    ],
    "Connections": [
        "manage_connections",
        "view_connections",
    ],
    "Queries": [
        "export_query",
    ],
    "Files": [
        "view_files",
        "upload_files",
        "delete_files",
    ],
    "Members & RBAC": [
        "view_organization_members",
        "add_organization_members",
        "update_organization_members",
        "remove_organization_members",
        "manage_roles",
        "manage_groups",
        "manage_role_assignments",
        "manage_resource_grants",
    ],
    "Instructions": [
        "view_instructions",
        "create_instructions",
        "update_instructions",
        "delete_instructions",
        "view_global_instructions",
        "view_hidden_instructions",
        "suggest_instructions",
    ],
    "Entities": [
        "view_entities",
        "create_entities",
        "update_entities",
        "delete_entities",
        "refresh_entities",
        "approve_entities",
        "reject_entities",
        "suggest_entities",
        "withdraw_entities",
    ],
    "Evals": [
        "manage_evals",
        "run_evals",
        "view_evals",
    ],
    "Builds": [
        "view_builds",
        "create_builds",
    ],
    "Settings": [
        "view_settings",
        "modify_settings",
        "view_organizations",
        "manage_organization_settings",
        "view_organization_settings",
        "view_organization_overview",
        "manage_organization_external_platforms",
        "manage_llm_settings",
        "view_llm_settings",
        "train_mode",
    ],
    "Feedback": [
        "create_completion_feedback",
        "view_all_completion_feedbacks",
    ],
    "Enterprise": [
        "view_audit_logs",
        "manage_scim",
        "manage_ldap",
    ],
}

# Flatten to get all valid permission strings
ALL_PERMISSIONS = set()
for perms in PERMISSION_CATEGORIES.values():
    ALL_PERMISSIONS.update(perms)

# ── Resource Permission Options ──────────────────────────────────────────
# Available permission strings for resource_grants by resource type

RESOURCE_PERMISSIONS = {
    "data_source": [
        "query",
        "view_schema",
        "view_entities",
        "create_entities",
        "view_instructions",
        "create_instructions",
        "view_evals",
        "run_evals",
        "manage",
        "manage_members",
    ],
    "connection": [
        "manage_data_sources",
        "manage",
    ],
    "report": [
        "view_artifacts",
        "view_conversation",
        "run_steps",
    ],
}

# ── Merged categories for the role editor UI ─────────────────────────────
# Groups related categories into fewer rows for a cleaner modal.
# Each merged group maps to the individual categories it contains.

MERGED_CATEGORIES = {
    "Reports & Queries": ["Reports", "Queries"],
    "Data & Connections": ["Data Sources", "Connections"],
    "Instructions & Entities": ["Instructions", "Entities"],
    "Evals": ["Evals"],
    "Members & Access": ["Members & RBAC"],
    "Settings & Admin": ["Settings", "Feedback", "Enterprise", "Builds", "Files"],
}

# Resource-scoped permission groups — shown per-resource in the role editor.
# Maps a resource type to labelled permission groups for the UI.

RESOURCE_SCOPED_GROUPS = {
    "data_source": {
        "Query": ["query", "view_schema"],
        "Instructions": ["view_instructions", "create_instructions"],
        "Entities": ["view_entities", "create_entities"],
        "Evals": ["view_evals", "run_evals"],
        "Management": ["manage", "manage_members"],
    },
    "connection": {
        "manage_data_sources": ["manage_data_sources"],
        "manage": ["manage"],
    },
    "report": {
        "Access": ["view_artifacts", "view_conversation", "run_steps"],
    },
}


# ── Default Role Permission Sets ─────────────────────────────────────────
# These define what the system-seeded admin and member roles contain.

DEFAULT_MEMBER_PERMISSIONS = [
    "view_data_source", "view_reports", "create_reports", "update_reports",
    "delete_reports", "publish_reports", "rerun_report_steps", "view_files",
    "upload_files", "delete_files", "export_query",
    "view_organizations", "view_llm_settings", "view_organization_members",
    "manage_organization_external_platforms", "view_instructions",
    "view_global_instructions",
    "suggest_instructions",
    "create_completion_feedback", "view_entities", "refresh_entities",
    "suggest_entities", "withdraw_entities", "view_evals", "run_evals",
    "view_builds",
]

# Admin uses full_admin_access wildcard — no need to list individual permissions
DEFAULT_ADMIN_PERMISSIONS = ["full_admin_access"]
