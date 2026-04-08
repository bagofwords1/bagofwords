"""
RBAC registry parity tests — pure static checks, no fixtures needed.

These tests walk the application source of truth (routes + the
``permissions_registry`` module + the ``permission_resolver``) and assert
the different pieces stay in sync. They catch regressions where a route
decorator references a perm that no longer exists in the registry (the
exact class of bug that broke the frontend with ``manage_tests``,
``modify_settings``, etc.).

Everything here runs offline — no HTTP calls, no DB — so the file has
near-zero runtime cost even when something else in the suite is broken.
"""
import ast
import re
from pathlib import Path

import pytest

from app.core import permissions_registry as registry
from app.core import permission_resolver as resolver


ROUTES_DIR = Path(__file__).resolve().parents[3] / "app" / "routes"
FULL_ADMIN = "full_admin_access"


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────


def _iter_requires_permission_calls(tree: ast.AST):
    """Yield every ast.Call node that decorates a function with @requires_permission(...)."""
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for deco in node.decorator_list:
            call = deco if isinstance(deco, ast.Call) else None
            if call is None:
                continue
            # Handle both `@requires_permission('x')` and `@module.requires_permission('x')`
            func = call.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name == "requires_permission":
                yield call, node


def _extract_literal_permissions(call: ast.Call):
    """Return the first positional arg as a list of literal permission strings.

    Supports:
        @requires_permission('view_reports')
        @requires_permission(('a', 'b'))
        @requires_permission(['a', 'b'])
    """
    if not call.args:
        return []
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    if isinstance(arg, (ast.Tuple, ast.List, ast.Set)):
        out = []
        for elt in arg.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                out.append(elt.value)
        return out
    return []


def _is_resource_scoped(call: ast.Call) -> bool:
    for kw in call.keywords:
        if kw.arg == "resource_scoped" and isinstance(kw.value, ast.Constant):
            return bool(kw.value.value)
    return False


def _collect_route_permissions():
    """Walk routes/*.py and collect (file, function, permission, resource_scoped) tuples."""
    findings = []
    for path in sorted(ROUTES_DIR.glob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as e:
            pytest.fail(f"Failed to parse {path}: {e}")
        for call, func in _iter_requires_permission_calls(tree):
            for perm in _extract_literal_permissions(call):
                findings.append((path.name, func.name, perm, _is_resource_scoped(call)))
    return findings


def _collect_check_resource_permissions_calls():
    """Walk routes/*.py for ``check_resource_permissions(... resource_type, ids, permission)``.

    The 4th positional argument is the resource_type literal; the 5th
    positional (or ``permission=`` kwarg) is the permission string. We
    only return literal-string occurrences — dynamic args we leave alone
    so we don't false-positive on legitimately variable usage.
    """
    findings = []
    for path in sorted(ROUTES_DIR.glob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as e:
            pytest.fail(f"Failed to parse {path}: {e}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = None
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                name = func.attr
            if name != "check_resource_permissions":
                continue

            # Layout: check_resource_permissions(db, user_id, org_id, resource_type, resource_ids, permission)
            # That's args[3] (resource_type) and args[5] (permission), positional.
            resource_type = None
            permission = None
            if len(node.args) >= 4 and isinstance(node.args[3], ast.Constant) and isinstance(node.args[3].value, str):
                resource_type = node.args[3].value
            if len(node.args) >= 6 and isinstance(node.args[5], ast.Constant) and isinstance(node.args[5].value, str):
                permission = node.args[5].value
            for kw in node.keywords:
                if kw.arg == "resource_type" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    resource_type = kw.value.value
                if kw.arg == "permission" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                    permission = kw.value.value
            if resource_type is not None and permission is not None:
                findings.append((path.name, resource_type, permission))
    return findings


# ────────────────────────────────────────────────────────────────────
# Registry self-consistency
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_all_permissions_is_nonempty():
    """Sanity: the permission registry is populated at import time."""
    assert isinstance(registry.ALL_PERMISSIONS, set)
    assert len(registry.ALL_PERMISSIONS) > 0, "ALL_PERMISSIONS must not be empty"


@pytest.mark.e2e
def test_default_member_permissions_subset_of_registry():
    """Every perm seeded on the default member role must be a known perm."""
    extras = set(registry.DEFAULT_MEMBER_PERMISSIONS) - registry.ALL_PERMISSIONS - {FULL_ADMIN}
    assert not extras, (
        f"DEFAULT_MEMBER_PERMISSIONS contains strings that are not in ALL_PERMISSIONS: {sorted(extras)}"
    )


@pytest.mark.e2e
def test_default_admin_permissions_subset_of_registry():
    """Admin's seed set should only contain full_admin_access or known strings."""
    extras = set(registry.DEFAULT_ADMIN_PERMISSIONS) - registry.ALL_PERMISSIONS - {FULL_ADMIN}
    assert not extras, (
        f"DEFAULT_ADMIN_PERMISSIONS contains unknown strings: {sorted(extras)}"
    )


@pytest.mark.e2e
def test_permission_categories_match_all_permissions():
    """Union of visible + hidden categories must equal ALL_PERMISSIONS."""
    union = set()
    for perms in registry.PERMISSION_CATEGORIES.values():
        union.update(perms)
    for perms in registry.HIDDEN_PERMISSION_CATEGORIES.values():
        union.update(perms)
    assert union == registry.ALL_PERMISSIONS, (
        f"PERMISSION_CATEGORIES ∪ HIDDEN_PERMISSION_CATEGORIES != ALL_PERMISSIONS\n"
        f"  missing: {sorted(registry.ALL_PERMISSIONS - union)}\n"
        f"  extra:   {sorted(union - registry.ALL_PERMISSIONS)}"
    )


@pytest.mark.e2e
def test_merged_categories_reference_known_buckets():
    """Every bucket referenced by MERGED_CATEGORIES must exist in PERMISSION_CATEGORIES."""
    known = set(registry.PERMISSION_CATEGORIES.keys())
    for group, buckets in registry.MERGED_CATEGORIES.items():
        for bucket in buckets:
            assert bucket in known, (
                f"MERGED_CATEGORIES[{group!r}] references missing bucket {bucket!r}; "
                f"known: {sorted(known)}"
            )


@pytest.mark.e2e
def test_resource_scoped_groups_reference_known_resource_perms():
    """RESOURCE_SCOPED_GROUPS values must all be valid per-resource permissions."""
    for resource_type, groups in registry.RESOURCE_SCOPED_GROUPS.items():
        valid = set(registry.RESOURCE_PERMISSIONS.get(resource_type, []))
        for group_name, perms in groups.items():
            for p in perms:
                assert p in valid, (
                    f"RESOURCE_SCOPED_GROUPS[{resource_type}][{group_name}] "
                    f"references unknown permission {p!r}; valid = {sorted(valid)}"
                )


# ────────────────────────────────────────────────────────────────────
# Route decorator parity
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_every_route_permission_is_registered():
    """No route decorator may reference a perm string that isn't in the registry.

    ``resource_scoped=True`` routes may declare their permission with a
    *resource*-level name (e.g. ``create_instructions``); accept those too.
    """
    all_org = registry.ALL_PERMISSIONS | {FULL_ADMIN}
    all_resource = set()
    for perms in registry.RESOURCE_PERMISSIONS.values():
        all_resource.update(perms)

    valid = all_org | all_resource

    findings = _collect_route_permissions()
    assert findings, "no @requires_permission calls were found — parser regression?"

    unknown = []
    for fname, func, perm, resource_scoped in findings:
        if perm in valid:
            continue
        unknown.append((fname, func, perm, resource_scoped))

    assert not unknown, (
        "These route decorators reference permission strings that don't exist "
        "in permissions_registry:\n  "
        + "\n  ".join(f"{f}::{func} → {perm!r} (resource_scoped={rs})" for f, func, perm, rs in unknown)
    )


@pytest.mark.e2e
def test_check_resource_permissions_uses_known_resource_perms():
    """Imperative check_resource_permissions(...) calls in routes/ must
    reference resource permissions that exist in the registry.

    This is the dual of ``test_every_route_permission_is_registered`` —
    that one walks ``@requires_permission`` decorators, this one walks
    in-body ``check_resource_permissions`` calls so we catch route-body
    drift like ``"create_evals"`` (which fails open as 403 for everyone
    except full_admin_access because the perm string isn't recognised
    anywhere in the resolver).

    Connection grants don't exist (``RESOURCE_PERMISSIONS`` is data_source
    only) but routes still reference connection-side permissions like
    ``manage_data_sources`` for connection-bound checks; we whitelist
    those to avoid false positives.
    """
    # Resource permissions whitelisted as legitimately not in
    # RESOURCE_PERMISSIONS (because they belong to other resource types
    # that the registry intentionally doesn't expose, e.g. connection).
    WHITELIST_PER_RESOURCE = {
        "connection": {"manage_data_sources"},
    }

    findings = _collect_check_resource_permissions_calls()
    unknown = []
    for fname, resource_type, perm in findings:
        valid = set(registry.RESOURCE_PERMISSIONS.get(resource_type, []))
        valid |= WHITELIST_PER_RESOURCE.get(resource_type, set())
        if perm not in valid:
            unknown.append((fname, resource_type, perm))

    assert not unknown, (
        "These check_resource_permissions(...) calls reference unknown "
        "resource permissions:\n  "
        + "\n  ".join(f"{f}: ({rt!r}, {p!r})" for f, rt, p in unknown)
    )


@pytest.mark.e2e
def test_org_perm_implies_resource_uses_known_org_perms():
    """Every org-level perm that triggers a resource implication must be registered."""
    for org_perm, mapping in resolver.ORG_PERM_IMPLIES_RESOURCE.items():
        assert org_perm in registry.ALL_PERMISSIONS or org_perm == FULL_ADMIN, (
            f"ORG_PERM_IMPLIES_RESOURCE key {org_perm!r} is not in ALL_PERMISSIONS"
        )
        for resource_type, implied in mapping.items():
            valid = set(registry.RESOURCE_PERMISSIONS.get(resource_type, []))
            unknown = set(implied) - valid
            assert not unknown, (
                f"ORG_PERM_IMPLIES_RESOURCE[{org_perm!r}][{resource_type!r}] implies "
                f"unknown resource permissions {sorted(unknown)}; valid = {sorted(valid)}"
            )


# ────────────────────────────────────────────────────────────────────
# Live endpoint parity
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_permissions_registry_endpoint_matches_module(test_client, create_user, login_user):
    """GET /permissions/registry must serve exactly what permissions_registry.py defines."""
    user = create_user()
    token = login_user(user["email"], user["password"])

    resp = test_client.get(
        "/api/permissions/registry",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()

    assert data["categories"] == registry.PERMISSION_CATEGORIES
    assert data["resource_permissions"] == registry.RESOURCE_PERMISSIONS
    assert data["merged_categories"] == registry.MERGED_CATEGORIES
    assert data["resource_scoped_groups"] == registry.RESOURCE_SCOPED_GROUPS


@pytest.mark.e2e
def test_registry_hides_reports_category(test_client, create_user, login_user):
    """Reports perms are valid strings but intentionally hidden from the UI."""
    user = create_user()
    token = login_user(user["email"], user["password"])

    resp = test_client.get(
        "/api/permissions/registry",
        headers={"Authorization": f"Bearer {token}"},
    )
    data = resp.json()
    assert "Reports" not in data["categories"], (
        "Reports should not appear in the visible categories response; it is "
        "defined in HIDDEN_PERMISSION_CATEGORIES on purpose so the role-editor "
        "UI does not render meaningless checkboxes."
    )
    # But the strings should still be registered internally
    assert "view_reports" in registry.ALL_PERMISSIONS
    assert "create_reports" in registry.ALL_PERMISSIONS
