"""
Central registry of all permission strings used in the application.

This is the single source of truth for valid permissions. Route decorators
reference these strings, the frontend receives them via whoami, and the
RolesManager UI groups them by category for the role editor.

Reports use legacy permission strings for backwards compatibility with
existing route decorators, but they are HIDDEN from the role editor UI:
view/create are effectively granted to all members; update/delete/publish
are gated by ownership in the route layer (see `owner_only=True` on the
report routes). The hidden category lets us keep the strings valid in the
resolver without exposing meaningless checkboxes in the UI.

Connection view is derived from data_source access; connection write is
gated by the org-level `manage_connections` permission. There are no
connection or report resource grants.
"""

# ── Org-level Permission Categories (visible in UI) ──────────────────────
# Used by the frontend RolesManager to group checkboxes.

PERMISSION_CATEGORIES = {
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
    "Entities": [
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
        "manage_identity_providers",
    ],
}

# Hidden categories: registered as valid permission strings (route decorators
# still reference them), seeded onto the member role, but excluded from the
# /permissions/registry response so they don't appear in the role editor.
HIDDEN_PERMISSION_CATEGORIES = {
    "Reports": [
        "view_reports",
        "create_reports",
        "update_reports",
        "delete_reports",
        "publish_reports",
    ],
}

# Flatten to get all valid permission strings (excludes the full_admin_access wildcard)
ALL_PERMISSIONS = set()
for perms in PERMISSION_CATEGORIES.values():
    ALL_PERMISSIONS.update(perms)
for perms in HIDDEN_PERMISSION_CATEGORIES.values():
    ALL_PERMISSIONS.update(perms)

# ── Resource Permission Options ──────────────────────────────────────────
# Available permission strings for resource_grants by resource type.
# data_source only — connection/report grants are intentionally not supported.

RESOURCE_PERMISSIONS = {
    "data_source": [
        "manage_instructions",
        "create_entities",
        "manage_evals",
        "manage",
        "manage_members",
    ],
}

# ── Merged categories for the role editor UI ─────────────────────────────
# Groups related categories into fewer rows for a cleaner modal.

MERGED_CATEGORIES = {
    "Files": ["Files"],
    "Data & Knowledge": ["Data & Connections", "Instructions", "Entities", "Evals"],
    "Members & Access": ["Members"],
    "Settings & Admin": ["Settings", "Enterprise"],
}

# Resource-scoped permission groups — shown per-resource in the role editor.
# Flat list (no Read/Full tiers) — the UI renders these as plain checkboxes.

RESOURCE_SCOPED_GROUPS = {
    "data_source": {
        "Permissions": [
            "manage_instructions",
            "create_entities",
            "manage_evals",
            "manage",
            "manage_members",
        ],
    },
}


# ── Default Role Permission Sets ─────────────────────────────────────────
# These define what the system-seeded admin and member roles contain.

# Member: baseline. Hidden report perms are granted so members can use the
# product; ownership/publication checks happen at the route layer.
DEFAULT_MEMBER_PERMISSIONS = [
    "view_reports",
    "create_reports",
    "update_reports",
    "delete_reports",
    "publish_reports",
    "manage_files",
    "view_members",
]

# Admin: gets all org perms via full_admin_access wildcard.
DEFAULT_ADMIN_PERMISSIONS = ["full_admin_access"]
