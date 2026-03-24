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
    "Widgets": [
        "view_widgets",
        "create_widgets",
        "update_widgets",
        "delete_widgets",
        "export_widgets",
        "create_text_widgets",
        "update_text_widgets",
        "view_text_widgets",
        "delete_text_widgets",
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
        "create_private_instructions",
        "update_private_instructions",
        "delete_private_instructions",
        "view_global_instructions",
        "view_private_instructions",
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
        "manage_tests",
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
        "manage",
        "manage_members",
        "create_instructions",
        "create_evals",
        "create_entities",
    ],
    "connection": [
        "use",
        "manage",
        "manage_credentials",
        "create_data_source",
    ],
    "report": [
        "view_artifacts",
        "view_conversation",
    ],
}

# ── Default Role Permission Sets ─────────────────────────────────────────
# These define what the system-seeded admin and member roles contain.

DEFAULT_MEMBER_PERMISSIONS = [
    "view_data_source", "view_reports", "create_reports", "update_reports",
    "delete_reports", "publish_reports", "rerun_report_steps", "view_files",
    "upload_files", "delete_files", "export_widgets", "create_text_widgets",
    "update_text_widgets", "view_text_widgets", "delete_text_widgets",
    "create_widgets", "update_widgets", "delete_widgets", "view_widgets",
    "view_organizations", "view_llm_settings", "view_organization_members",
    "manage_organization_external_platforms", "view_instructions",
    "create_private_instructions", "update_private_instructions",
    "delete_private_instructions", "view_global_instructions",
    "view_private_instructions", "suggest_instructions",
    "create_completion_feedback", "view_entities", "refresh_entities",
    "suggest_entities", "withdraw_entities", "view_builds",
]

# Admin uses full_admin_access wildcard — no need to list individual permissions
DEFAULT_ADMIN_PERMISSIONS = ["full_admin_access"]
