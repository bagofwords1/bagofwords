"""
Central registry of all permission strings used in the application.

This is the single source of truth for valid permissions. Route decorators
reference these strings, the frontend receives them via whoami, and the
RolesManager UI groups them by category for the role editor.

Some permissions are HIDDEN from the role editor UI and granted to every org
member instead — see BASELINE_PERMISSIONS below. Reports are hidden because
view/create are baseline product usage and update/delete/publish are gated by
ownership in the route layer (see `owner_only=True` on the report routes), so
a checkbox for them would be meaningless. Files are hidden for the same
reason: attaching a file to a chat and reading it back is what an ordinary
member does, not an admin privilege.

Connection view is derived from data_source access; connection write is
gated by the org-level `manage_connections` permission. There are no
connection or report resource grants.
"""

# ── Org-level Permission Categories (visible in UI) ──────────────────────
# Used by the frontend RolesManager to group checkboxes.

PERMISSION_CATEGORIES = {
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
        "manage_members",
        "manage_service_accounts",
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
# still reference them) and granted to every org member via
# BASELINE_PERMISSIONS, but excluded from the /permissions/registry response so
# they don't appear in the role editor.
HIDDEN_PERMISSION_CATEGORIES = {
    "Reports": [
        "view_reports",
        "create_reports",
        "update_reports",
        "delete_reports",
        "publish_reports",
    ],
    # view_members is baseline for any authenticated org member.
    "Members": [
        "view_members",
    ],
    # Uploading a file to a chat, listing files to @-mention one, and reading
    # file content back (which is also how images render in a conversation).
    "Files": [
        "manage_files",
    ],
}

# Flatten to get all valid permission strings (excludes the full_admin_access wildcard)
ALL_PERMISSIONS = set()
for perms in PERMISSION_CATEGORIES.values():
    ALL_PERMISSIONS.update(perms)
for perms in HIDDEN_PERMISSION_CATEGORIES.values():
    ALL_PERMISSIONS.update(perms)

# ── Baseline permissions ─────────────────────────────────────────────────
# Granted by the resolver to every human member of an organization, on top of
# whatever their roles carry (see ``_resolve_permissions_inner``).
#
# Hidden ⇒ baseline, and the two sets are deliberately the same set. That
# equivalence is what makes hiding a permission safe: a hidden permission is
# absent from the role editor, so the editor cannot grant it — if it were not
# baseline it would be reachable only through the seeded `member` role, and any
# custom role would silently produce a user who cannot open a report or attach
# a file to a chat.
#
# To make a permission withholdable, move it out of
# HIDDEN_PERMISSION_CATEGORIES into PERMISSION_CATEGORIES so a role can
# actually grant it. Do not add a permission to one of these without the other.
#
# Service accounts are excluded (they have no Membership row): an API key holds
# exactly what its role grants. New service accounts default to the `member`
# role, which is seeded with this same set.
BASELINE_PERMISSIONS = sorted(
    p for perms in HIDDEN_PERMISSION_CATEGORIES.values() for p in perms
)

# ── Resource Permission Options ──────────────────────────────────────────
# Available permission strings for resource_grants by resource type.
#
# `connection` grants let an admin delegate, per connection:
#   - manage_connection    → edit config / test / reindex / delete the connection
#   - create_data_sources  → create agents on this connection
#   - manage_data_sources  → manage ALL agents on this connection (implies
#                            create_data_sources; cascades to per-agent `manage`)

RESOURCE_PERMISSIONS = {
    "data_source": [
        "manage_instructions",
        "create_entities",
        "manage_evals",
        "manage",
        "manage_members",
    ],
    "connection": [
        "manage_connection",
        "create_data_sources",
        "manage_data_sources",
    ],
    # Projects (shared report folders). `view` = see the project and read its
    # reports; `manage` = edit the project, its defaults and its members.
    # Granted per-user/group when a project is shared; not exposed in the
    # role editor (sharing UI manages these grants directly).
    "project": [
        "view",
        "manage",
    ],
}

# ── Merged categories for the role editor UI ─────────────────────────────
# Groups related categories into fewer rows for a cleaner modal.

MERGED_CATEGORIES = {
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
    "connection": {
        "Permissions": [
            "manage_connection",
            "create_data_sources",
            "manage_data_sources",
        ],
    },
}


# ── Default Role Permission Sets ─────────────────────────────────────────
# These define what the system-seeded admin and member roles contain.

# Member: exactly the baseline set, nothing more. Seeded explicitly so the
# `member` role row is self-describing in the database (and so service accounts,
# which default to this role and get no baseline grant, still hold it), even
# though the resolver grants the same strings to every member regardless of role.
DEFAULT_MEMBER_PERMISSIONS = list(BASELINE_PERMISSIONS)

# Admin: gets all org perms via full_admin_access wildcard.
DEFAULT_ADMIN_PERMISSIONS = ["full_admin_access"]
