"""Unit tests for the MVP permissions registry."""
from app.core.permissions_registry import (
    ALL_PERMISSIONS,
    BASELINE_PERMISSIONS,
    DEFAULT_ADMIN_PERMISSIONS,
    DEFAULT_MEMBER_PERMISSIONS,
    DEFAULT_ON_PERMISSIONS,
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
    "view_code", "run_custom_code",
    "view_members", "manage_members", "manage_service_accounts",
    "manage_settings", "manage_llm",
    "view_audit_logs", "manage_identity_providers",
}


def test_all_permissions_is_exactly_expected_set():
    assert ALL_PERMISSIONS == EXPECTED_ORG_PERMS
    assert len(ALL_PERMISSIONS) == 20


def test_full_admin_access_is_not_in_all_permissions():
    # Wildcard is intentionally separate from the enumerated set
    assert "full_admin_access" not in ALL_PERMISSIONS


def test_resource_permissions_cover_the_grantable_resource_types():
    assert set(RESOURCE_PERMISSIONS.keys()) == {"data_source", "connection", "project"}
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


def test_files_are_hidden_from_visible_categories():
    # manage_files is baseline product usage, not a withholdable admin
    # privilege — no checkbox in the role editor.
    assert "Files" not in PERMISSION_CATEGORIES
    assert HIDDEN_PERMISSION_CATEGORIES["Files"] == ["manage_files"]
    visible = {p for perms in PERMISSION_CATEGORIES.values() for p in perms}
    assert "manage_files" not in visible


def test_hidden_permissions_are_exactly_the_baseline_set():
    """The invariant that makes hiding safe: a permission the role editor cannot
    grant must be granted to every member instead, or it is unreachable."""
    hidden = {p for perms in HIDDEN_PERMISSION_CATEGORIES.values() for p in perms}
    assert set(BASELINE_PERMISSIONS) == hidden


def test_baseline_permissions_are_valid_and_never_the_wildcard():
    assert set(BASELINE_PERMISSIONS) <= ALL_PERMISSIONS
    assert "full_admin_access" not in BASELINE_PERMISSIONS


def test_member_role_is_seeded_with_baseline_plus_default_on():
    """The member seed is a strict SUPERSET of the baseline set.

    These two were identical until code visibility became withholdable. They
    can no longer be equal: a baseline permission is granted to every member by
    the resolver and therefore cannot be withheld by any role, so `view_code`
    had to live outside the baseline while still being on by default. Asserting
    the intended relationship keeps that distinction honest — equality here
    would mean someone quietly made a withholdable permission ungrantable.
    """
    assert set(BASELINE_PERMISSIONS) < set(DEFAULT_MEMBER_PERMISSIONS)
    assert set(DEFAULT_MEMBER_PERMISSIONS) == (
        set(BASELINE_PERMISSIONS) | set(DEFAULT_ON_PERMISSIONS)
    )


def test_default_on_permissions_are_grantable_not_baseline():
    """Each default-on permission must be withholdable, i.e. visible in the
    role editor and absent from the baseline. A permission that is both
    default-on and baseline is a checkbox that does nothing."""
    for perm in DEFAULT_ON_PERMISSIONS:
        assert perm in ALL_PERMISSIONS, f"{perm} is not a registered permission"
        assert perm not in BASELINE_PERMISSIONS, (
            f"{perm} is baseline, so no role can withhold it"
        )
        visible = {p for perms in PERMISSION_CATEGORIES.values() for p in perms}
        assert perm in visible, f"{perm} is not visible in the role editor"


def test_code_permissions_are_registered():
    assert PERMISSION_CATEGORIES["Code"] == ["view_code", "run_custom_code"]


def test_instructions_and_entities_are_separate_categories():
    assert "Instructions" in PERMISSION_CATEGORIES
    assert "Entities" in PERMISSION_CATEGORIES
    assert PERMISSION_CATEGORIES["Instructions"] == ["manage_instructions"]
    assert PERMISSION_CATEGORIES["Entities"] == ["manage_entities"]
