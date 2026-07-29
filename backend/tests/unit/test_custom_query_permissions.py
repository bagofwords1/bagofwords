"""Who may do what to a custom query.

Two distinct rights, deliberately not the same one:

  * **Authoring** a custom query — its SQL, its schedule, and its RLS policy —
    is a CONNECTION-admin act. The SQL runs with the connection's own
    credential, and the policy decides what every agent on that connection can
    read. That is `manage_connection`.
  * **Activating** an existing relation for one agent is an AGENT-admin act:
    it chooses whether this agent may see something an admin already
    authorised. That is `data_source:manage`.

An agent manager who could author queries would be able to run arbitrary SQL
under a credential they were never given, so the split matters. These tests
assert it structurally — a route added later without the decorator fails here
rather than shipping open.
"""

import inspect
import re

import pytest


CUSTOM_QUERY_GUARD = "@requires_resource_permission('connection', 'manage_connection')"
ACTIVATION_GUARD = "@requires_resource_permission('data_source', 'manage')"


def _routes(module, path_fragment):
    """(method, path, [decorators]) for each route whose path matches."""
    src = inspect.getsource(module).splitlines()
    found = []
    for i, line in enumerate(src):
        stripped = line.strip()
        if not stripped.startswith("@router.") or path_fragment not in stripped:
            continue
        m = re.search(r'@router\.(\w+)\(\s*"([^"]+)"', stripped)
        if not m:
            continue
        decorators = []
        for j in range(i + 1, min(i + 8, len(src))):
            t = src[j].strip()
            if t.startswith(("async def", "def ")):
                break
            if t.startswith("@"):
                decorators.append(t)
        found.append((m.group(1).upper(), m.group(2), decorators))
    return found


def test_every_custom_query_route_requires_connection_admin():
    from app.routes import connection

    routes = _routes(connection, "custom-queries")
    assert len(routes) >= 9, f"expected the full custom-query surface, found {len(routes)}"

    unguarded = [
        f"{method} {path}"
        for method, path, decorators in routes
        if CUSTOM_QUERY_GUARD not in decorators
    ]
    assert not unguarded, (
        "these custom-query routes do not require manage_connection: " + ", ".join(unguarded)
    )


def test_the_rls_routes_are_among_them():
    """Named explicitly because RLS is the newest surface and the one where an
    unguarded route leaks other people's rows rather than merely config."""
    from app.routes import connection

    paths = {path for _, path, _ in _routes(connection, "custom-queries")}
    assert any(p.endswith("/rls") for p in paths), paths
    assert any(p.endswith("/rls/preview") for p in paths), paths
    assert any(p.endswith("/rls-options") for p in paths), paths


def test_preview_as_user_is_connection_admin_only():
    """It returns another member's rows by design. Anything less than
    connection-admin turns it into an impersonation endpoint."""
    from app.routes import connection

    route = next(
        (r for r in _routes(connection, "custom-queries") if r[1].endswith("/rls/preview")),
        None,
    )
    assert route is not None
    assert CUSTOM_QUERY_GUARD in route[2]


def test_activation_is_an_agent_permission_not_a_connection_one():
    """Turning a relation on for one agent is a lesser right than authoring it.
    Requiring manage_connection here would mean only connection admins could
    wire up their own agents."""
    from app.routes import data_source

    routes = _routes(data_source, "update_tables_status") + _routes(
        data_source, "bulk_update_tables"
    )
    assert routes, "the activation routes moved — re-point this test"
    for method, path, decorators in routes:
        assert ACTIVATION_GUARD in decorators, f"{method} {path} is not agent-manage guarded"
        assert CUSTOM_QUERY_GUARD not in decorators, (
            f"{method} {path} requires connection-admin; agent managers could not "
            f"activate a relation on their own agent"
        )


def test_the_beta_gate_is_checked_by_every_custom_query_route():
    """`enable_custom_queries` is off by default. A route that skips the gate
    exposes the feature to orgs that never turned it on."""
    from app.routes import connection

    src = inspect.getsource(connection)
    # Each handler body should call ensure_enabled before doing any work.
    bodies = src.split("@router.")
    missing = []
    for body in bodies:
        first = body.splitlines()[0] if body.splitlines() else ""
        if "custom-queries" not in first:
            continue
        if "ensure_enabled" not in body:
            missing.append(first.strip())
    assert not missing, f"custom-query routes not behind the beta gate: {missing}"


@pytest.mark.asyncio
async def test_the_service_gate_fails_closed():
    """If the settings lookup itself breaks, the answer must be 'disabled'."""
    from fastapi import HTTPException

    from app.services.custom_query_service import custom_query_service

    class BrokenOrg:
        id = "org"

        async def get_settings(self, db):
            raise RuntimeError("settings unavailable")

    with pytest.raises(HTTPException) as e:
        await custom_query_service.ensure_enabled(None, BrokenOrg())
    assert e.value.status_code in (403, 404)
