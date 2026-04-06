"""Unit tests for the MVP permissions registry."""
from app.core.permissions_registry import (
    ALL_PERMISSIONS,
    DEFAULT_ADMIN_PERMISSIONS,
    DEFAULT_MEMBER_PERMISSIONS,
    MERGED_CATEGORIES,
    MVP_OLD_TO_NEW_PERM_MAP,
    PERMISSION_CATEGORIES,
    RESOURCE_PERMISSIONS,
    RESOURCE_SCOPED_GROUPS,
)


EXPECTED_ORG_PERMS = {
    "view_reports", "create_reports", "update_reports", "delete_reports", "publish_reports",
    "manage_files",
    "create_data_source", "manage_connections",
    "manage_instructions",
    "manage_entities",
    "manage_evals",
    "view_members", "manage_members",
    "manage_settings", "manage_llm",
    "view_audit_logs", "manage_scim", "manage_ldap",
}


def test_all_permissions_is_exactly_mvp_set():
    assert ALL_PERMISSIONS == EXPECTED_ORG_PERMS
    assert len(ALL_PERMISSIONS) == 18


def test_full_admin_access_is_not_in_all_permissions():
    # Wildcard is intentionally separate from the enumerated set
    assert "full_admin_access" not in ALL_PERMISSIONS


def test_resource_permissions_only_data_source_in_mvp():
    assert set(RESOURCE_PERMISSIONS.keys()) == {"data_source"}
    assert set(RESOURCE_PERMISSIONS["data_source"]) == {
        "view", "view_schema",
        "create_instructions",
        "create_entities",
        "manage_evals",
        "manage", "manage_members",
    }


def test_resource_scoped_groups_cover_all_data_source_perms():
    flat = {p for group in RESOURCE_SCOPED_GROUPS["data_source"].values() for p in group}
    assert flat == set(RESOURCE_PERMISSIONS["data_source"])


def test_merged_categories_reference_real_categories():
    for merged, children in MERGED_CATEGORIES.items():
        for child in children:
            assert child in PERMISSION_CATEGORIES, f"{merged} references unknown {child}"


def test_default_member_permissions_are_valid():
    for p in DEFAULT_MEMBER_PERMISSIONS:
        assert p in ALL_PERMISSIONS, f"DEFAULT_MEMBER_PERMISSIONS has invalid perm: {p}"


def test_default_admin_uses_wildcard():
    assert DEFAULT_ADMIN_PERMISSIONS == ["full_admin_access"]


def test_mvp_map_targets_are_valid_or_none():
    for old, new in MVP_OLD_TO_NEW_PERM_MAP.items():
        if new is None:
            continue
        assert new in ALL_PERMISSIONS, (
            f"MVP_OLD_TO_NEW_PERM_MAP[{old!r}] -> {new!r} is not a valid MVP permission"
        )


def test_mvp_map_covers_key_legacy_perms():
    # Sanity: the perms referenced by existing decorators must all have a mapping
    legacy_must_map = [
        "view_data_source", "update_data_source", "delete_data_source",
        "view_instructions", "create_instructions", "update_instructions", "delete_instructions",
        "view_files", "upload_files", "delete_files",
        "view_organization_members", "add_organization_members",
        "run_evals", "view_evals",
        "train_mode", "export_query",
    ]
    for perm in legacy_must_map:
        assert perm in MVP_OLD_TO_NEW_PERM_MAP, f"{perm} missing from MVP_OLD_TO_NEW_PERM_MAP"


def test_categories_flat_equals_all_permissions():
    flat = {p for perms in PERMISSION_CATEGORIES.values() for p in perms}
    assert flat == ALL_PERMISSIONS
