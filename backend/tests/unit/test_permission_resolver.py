"""Unit tests for ResolvedPermissions and the ORG_PERM_IMPLIES_RESOURCE tier logic."""
from app.core.permission_resolver import (
    FULL_ADMIN,
    ORG_PERM_IMPLIES_RESOURCE,
    ResolvedPermissions,
)


# ── has_org_permission ───────────────────────────────────────────────────

def test_has_org_permission_direct():
    rp = ResolvedPermissions(org_permissions={"view_reports"})
    assert rp.has_org_permission("view_reports")
    assert not rp.has_org_permission("manage_members")


def test_full_admin_bypasses_org_check():
    rp = ResolvedPermissions(org_permissions={FULL_ADMIN})
    assert rp.has_org_permission("manage_members")
    assert rp.has_org_permission("anything_at_all")


# ── has_resource_permission: tier 1 (full_admin) ─────────────────────────

def test_full_admin_bypasses_resource_check():
    rp = ResolvedPermissions(org_permissions={FULL_ADMIN})
    assert rp.has_resource_permission("data_source", "ds-1", "manage_instructions")
    assert rp.has_resource_permission("data_source", "ds-1", "manage")


# ── has_resource_permission: tier 2 (ORG_PERM_IMPLIES_RESOURCE) ──────────

def test_manage_instructions_implies_manage_instructions_on_any_ds():
    rp = ResolvedPermissions(org_permissions={"manage_instructions"})
    assert rp.has_resource_permission("data_source", "ds-1", "manage_instructions")
    assert rp.has_resource_permission("data_source", "ds-999", "manage_instructions")


def test_manage_entities_implies_create_entities_on_any_ds():
    rp = ResolvedPermissions(org_permissions={"manage_entities"})
    assert rp.has_resource_permission("data_source", "ds-1", "create_entities")
    # Does NOT cross over to instructions
    assert not rp.has_resource_permission("data_source", "ds-1", "manage_instructions")


def test_manage_evals_implies_manage_evals_on_any_ds():
    rp = ResolvedPermissions(org_permissions={"manage_evals"})
    assert rp.has_resource_permission("data_source", "ds-1", "manage_evals")


def test_org_perm_implication_does_not_grant_unrelated_resource_perms():
    rp = ResolvedPermissions(org_permissions={"manage_instructions"})
    # `manage` and `view` are not implied by manage_instructions
    assert not rp.has_resource_permission("data_source", "ds-1", "manage")
    assert not rp.has_resource_permission("data_source", "ds-1", "view")


# ── has_resource_permission: tier 3 (explicit grants) ────────────────────

def test_explicit_grant_allows_resource_permission():
    rp = ResolvedPermissions(
        resource_permissions={("data_source", "ds-1"): {"manage_instructions"}},
    )
    assert rp.has_resource_permission("data_source", "ds-1", "manage_instructions")


def test_explicit_grant_is_scoped_to_resource_id():
    rp = ResolvedPermissions(
        resource_permissions={("data_source", "ds-1"): {"manage_instructions"}},
    )
    assert not rp.has_resource_permission("data_source", "ds-2", "manage_instructions")


# ── has_resource_permission: `manage` grant superset (RESOURCE_PERM_IMPLIES) ──

def test_manage_grant_implies_management_subpermissions_on_same_ds():
    """An agent owner/manager (holding `manage`) can manage that agent's
    instructions, entities, evals and membership without extra grants."""
    rp = ResolvedPermissions(
        resource_permissions={("data_source", "ds-1"): {"manage"}},
    )
    for perm in ("manage_instructions", "create_entities", "manage_evals",
                 "manage_members", "view", "view_schema", "manage"):
        assert rp.has_resource_permission("data_source", "ds-1", perm), perm


def test_manage_grant_superset_is_scoped_to_its_own_ds():
    """`manage` on ds-1 grants nothing on ds-2 — managers are scoped to their
    own agents, unlike the org-level manage_* permissions."""
    rp = ResolvedPermissions(
        resource_permissions={("data_source", "ds-1"): {"manage"}},
    )
    assert not rp.has_resource_permission("data_source", "ds-2", "manage_instructions")
    assert not rp.has_resource_permission("data_source", "ds-2", "create_entities")


def test_non_manage_grant_does_not_imply_management_subpermissions():
    """A view-only grant must NOT confer management permissions."""
    rp = ResolvedPermissions(
        resource_permissions={("data_source", "ds-1"): {"view", "view_schema"}},
    )
    assert not rp.has_resource_permission("data_source", "ds-1", "manage_instructions")
    assert not rp.has_resource_permission("data_source", "ds-1", "create_entities")
    assert not rp.has_resource_permission("data_source", "ds-1", "manage")


# ── has_any_resource_permission (resource-scoped route pre-filter) ────────────

def test_has_any_resource_permission_honours_manage_superset():
    rp = ResolvedPermissions(
        resource_permissions={("data_source", "ds-1"): {"manage"}},
    )
    # manage on some agent ⇒ passes the resource-scoped pre-filter for these.
    assert rp.has_any_resource_permission("manage_instructions")
    assert rp.has_any_resource_permission("create_entities")
    assert rp.has_any_resource_permission("manage_evals")


def test_has_any_resource_permission_explicit_and_negative():
    rp = ResolvedPermissions(
        resource_permissions={("data_source", "ds-1"): {"manage_instructions"}},
    )
    assert rp.has_any_resource_permission("manage_instructions")
    # No grant implies create_entities here.
    assert not rp.has_any_resource_permission("create_entities")
    # Empty set → nothing.
    assert not ResolvedPermissions().has_any_resource_permission("manage_instructions")


def test_full_admin_passes_any_resource_prefilter():
    rp = ResolvedPermissions(org_permissions={FULL_ADMIN})
    assert rp.has_any_resource_permission("manage_instructions")


def test_no_grant_no_org_perm_denies():
    rp = ResolvedPermissions()
    assert not rp.has_resource_permission("data_source", "ds-1", "manage_instructions")


# ── has_resource_membership ──────────────────────────────────────────────

def test_has_resource_membership_for_explicit_grant():
    rp = ResolvedPermissions(
        resource_permissions={("data_source", "ds-1"): {"view"}},
    )
    assert rp.has_resource_membership("data_source", "ds-1")
    assert not rp.has_resource_membership("data_source", "ds-2")


def test_full_admin_implies_resource_membership():
    rp = ResolvedPermissions(org_permissions={FULL_ADMIN})
    assert rp.has_resource_membership("data_source", "any-ds")


# ── ORG_PERM_IMPLIES_RESOURCE shape ──────────────────────────────────────

def test_org_perm_implies_resource_map_targets_are_valid_resource_perms():
    from app.core.permissions_registry import RESOURCE_PERMISSIONS
    for org_perm, by_type in ORG_PERM_IMPLIES_RESOURCE.items():
        for resource_type, implied_perms in by_type.items():
            assert resource_type in RESOURCE_PERMISSIONS, (
                f"{org_perm} implies unknown resource type {resource_type}"
            )
            for p in implied_perms:
                assert p in RESOURCE_PERMISSIONS[resource_type], (
                    f"{org_perm} implies {p} which is not a valid {resource_type} grant"
                )
