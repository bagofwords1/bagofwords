"""
Central registry of all permission strings used in the application.

This is the single source of truth for valid permissions. Route decorators
reference these strings, the frontend receives them via whoami, and the
RolesManager UI groups them by category for the role editor.

MVP scope: 18 org-level permissions + 7 data_source resource grants, plus
the `full_admin_access` wildcard. Reports/builds/widgets are derived from
data_source access. View of instructions/entities is derived from DS view. Connection and report resource grants
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
        "manage_entities",
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
        "create_instructions",
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
        "Instructions": ["create_instructions"],
        "Entities": ["create_entities"],
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


