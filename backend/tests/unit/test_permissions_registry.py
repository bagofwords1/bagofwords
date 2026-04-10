"""Unit tests for the MVP permissions registry."""
from app.core.permissions_registry import (
    ALL_PERMISSIONS,
    DEFAULT_ADMIN_PERMISSIONS,
    DEFAULT_MEMBER_PERMISSIONS,
    HIDDEN_PERMISSION_CATEGORIES,
    MERGED_CATEGORIES,
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
    "view_audit_logs", "manage_identity_providers",
}


def test_all_permissions_is_exactly_mvp_set():
    assert ALL_PERMISSIONS == EXPECTED_ORG_PERMS
    assert len(ALL_PERMISSIONS) == 17


def test_full_admin_access_is_not_in_all_permissions():
    # Wildcard is intentionally separate from the enumerated set
    assert "full_admin_access" not in ALL_PERMISSIONS


def test_resource_permissions_only_data_source_in_mvp():
    assert set(RESOURCE_PERMISSIONS.keys()) == {"data_source"}
    assert set(RESOURCE_PERMISSIONS["data_source"]) == {
        "manage_instructions",
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


def test_categories_flat_equals_all_permissions():
    flat = {p for perms in PERMISSION_CATEGORIES.values() for p in perms}
    flat |= {p for perms in HIDDEN_PERMISSION_CATEGORIES.values() for p in perms}
    assert flat == ALL_PERMISSIONS


def test_reports_are_hidden_from_visible_categories():
    assert "Reports" not in PERMISSION_CATEGORIES
    assert "Reports" in HIDDEN_PERMISSION_CATEGORIES


def test_instructions_and_entities_are_separate_categories():
    assert "Instructions" in PERMISSION_CATEGORIES
    assert "Entities" in PERMISSION_CATEGORIES
    assert PERMISSION_CATEGORIES["Instructions"] == ["manage_instructions"]
    assert PERMISSION_CATEGORIES["Entities"] == ["manage_entities"]
