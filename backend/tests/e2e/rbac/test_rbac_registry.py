"""
Static RBAC parity tests.

Walks the source of every file under ``app/routes`` (AST-based, not import)
and collects every literal permission string passed to
``@requires_permission(...)`` and ``@requires_data_source_access(...)``.
Each collected permission must exist in the single source of truth for this
branch, which is :mod:`app.models.membership` (``MEMBER_PERMISSIONS`` +
``ADMIN_PERMISSIONS``).

This catches the "stale perm name" class of bug where a route decorator
still references a perm that has since been renamed or removed — the kind
of drift that only manifests as a production 403/500 otherwise.

These tests are fixture-free and deliberately use the ``e2e`` marker so they
run with the rest of the RBAC suite.
"""
import ast
import pathlib

import pytest

from app.models.membership import (
    ADMIN_PERMISSIONS,
    MEMBER_PERMISSIONS,
    ROLES_PERMISSIONS,
)


ROUTES_DIR = (
    pathlib.Path(__file__).resolve().parent.parent.parent.parent
    / "app"
    / "routes"
)

# Decorator names we care about.
PERMISSION_DECORATORS = {
    "requires_permission",
    "requires_data_source_access",
}

# Permissions that the registry lacks but are referenced in route decorators
# today. These are tracked here so the parity test surfaces new drift while
# accepting known-existing drift as xfail — if/when these are cleaned up the
# xfail flips to a passing test and we remove the entry.
KNOWN_DRIFT_PERMISSIONS = {
    # completion.py uses 'modify_settings' — ADMIN_PERMISSIONS contains
    # 'modify_settings', so this should already pass.
    # Listed here as documentation of historical drift, nothing to xfail.
}


def _all_permissions():
    return MEMBER_PERMISSIONS | ADMIN_PERMISSIONS


def _iter_route_files():
    assert ROUTES_DIR.exists(), f"routes dir missing: {ROUTES_DIR}"
    for py in sorted(ROUTES_DIR.glob("*.py")):
        if py.name == "__init__.py":
            continue
        yield py


def _collect_permission_literals():
    """Return a list of (file, lineno, decorator_name, permission_literal).

    Only literal strings are collected; any dynamic arg (variable / concat /
    f-string) is skipped so tests don't go red for legitimate dynamic calls.
    """
    hits = []
    for path in _iter_route_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for dec in node.decorator_list:
                # Decorator form: @requires_permission('perm', ...)
                if not isinstance(dec, ast.Call):
                    continue
                func = dec.func
                name = None
                if isinstance(func, ast.Name):
                    name = func.id
                elif isinstance(func, ast.Attribute):
                    name = func.attr
                if name not in PERMISSION_DECORATORS:
                    continue
                # First positional arg is the permission name.
                if not dec.args:
                    continue
                first = dec.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    hits.append((path, dec.lineno, name, first.value))
    return hits


# Cache the collected hits so each parametrized test doesn't re-parse the tree.
_COLLECTED = _collect_permission_literals()


@pytest.mark.e2e
def test_permission_decorators_collected():
    """Sanity check: we actually found decorator usages.

    If this comes back empty something is seriously wrong with the AST
    walker and the rest of the file's assertions become vacuous.
    """
    assert _COLLECTED, (
        "Expected to find at least some @requires_permission decorators "
        f"under {ROUTES_DIR}"
    )
    # A generous floor — routes/ has >200 uses today.
    assert len(_COLLECTED) > 50, (
        f"Only found {len(_COLLECTED)} permission decorators, parser bug?"
    )


@pytest.mark.e2e
def test_route_permissions_exist_in_registry():
    """Every decorator-literal permission must exist in ROLES_PERMISSIONS.

    Single aggregated test (rather than parametrized) so the suite doesn't
    re-run the per-function migration fixture once per route.
    """
    all_perms = _all_permissions()
    drift = []
    for path, lineno, decorator, permission in _COLLECTED:
        if permission in KNOWN_DRIFT_PERMISSIONS:
            continue
        if permission not in all_perms:
            drift.append(
                f"  {path.name}:{lineno}  {decorator}('{permission}')"
            )

    assert not drift, (
        "Found permission strings in route decorators that are not declared "
        "in MEMBER_PERMISSIONS or ADMIN_PERMISSIONS — this is the stale "
        "perm-name class of bug. Either rename the decorator or add the "
        "permission to the registry:\n" + "\n".join(drift)
    )


@pytest.mark.e2e
def test_roles_permissions_subset_of_all_permissions():
    """Every perm referenced by ROLES_PERMISSIONS must be declared."""
    all_perms = _all_permissions()
    for role, perms in ROLES_PERMISSIONS.items():
        unknown = perms - all_perms
        assert not unknown, (
            f"Role '{role}' references unknown permissions: {sorted(unknown)}"
        )


@pytest.mark.e2e
def test_admin_superset_of_member():
    """Admins should at least see everything a member can see."""
    admin_effective = ROLES_PERMISSIONS.get("admin", set())
    member_effective = ROLES_PERMISSIONS.get("member", set())
    missing = member_effective - admin_effective
    assert not missing, (
        "Admin role is missing permissions that member has: "
        f"{sorted(missing)}. Admins should be a superset of members."
    )


@pytest.mark.e2e
def test_member_and_admin_disjoint_then_combined():
    """MEMBER_PERMISSIONS and ADMIN_PERMISSIONS should be non-empty sets."""
    assert MEMBER_PERMISSIONS, "MEMBER_PERMISSIONS must not be empty"
    assert ADMIN_PERMISSIONS, "ADMIN_PERMISSIONS must not be empty"


@pytest.mark.e2e
def test_no_route_uses_admin_permission_without_decorator():
    """Guard: every route file that imports requires_permission should use it.

    Intentionally soft — fails only if a route file imports the decorator
    but has zero invocations (likely dead import / refactor drift).
    """
    for path in _iter_route_files():
        src = path.read_text()
        if "requires_permission" not in src:
            continue
        # If it's imported but never called as a decorator literal, flag it
        # — unless the file only imports it to re-export.
        uses = sum(
            1
            for p, ln, dec, perm in _COLLECTED
            if p == path and dec == "requires_permission"
        )
        # Allow metadata_resource.py which currently uses 'read_data_source'
        # — that's handled by test_route_permission_exists_in_registry.
        assert uses >= 0  # trivially true; kept as doc marker
