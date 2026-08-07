"""
Unit tests for OIDC group sync service and Graph API helper.

Tests the core logic without database or HTTP calls.
"""
import pytest
import json
import httpx
from unittest.mock import AsyncMock, patch, MagicMock


# ── Graph API helper tests ──


@pytest.mark.asyncio
async def test_resolve_group_names_from_graph():
    """Graph /me/memberOf returns group ID → displayName mapping."""
    graph_response = {
        "value": [
            {
                "@odata.type": "#microsoft.graph.group",
                "id": "aaa-bbb-ccc",
                "displayName": "AllFabric",
            },
            {
                "@odata.type": "#microsoft.graph.group",
                "id": "ddd-eee-fff",
                "displayName": "MinimalFabric",
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Bearer fake_token" in request.headers["authorization"]
        return httpx.Response(200, json=graph_response)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    with patch.object(httpx, "AsyncClient", _Patched):
        from app.ee.oidc.graph_client import resolve_group_names
        result = await resolve_group_names("fake_token")

    assert result == {"aaa-bbb-ccc": "AllFabric", "ddd-eee-fff": "MinimalFabric"}


@pytest.mark.asyncio
async def test_resolve_group_names_filters_non_groups():
    """Graph memberOf may return directoryRoles — only groups are extracted."""
    graph_response = {
        "value": [
            {
                "@odata.type": "#microsoft.graph.group",
                "id": "aaa-bbb-ccc",
                "displayName": "AllFabric",
            },
            {
                "@odata.type": "#microsoft.graph.directoryRole",
                "id": "role-123",
                "displayName": "Global Administrator",
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=graph_response)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    with patch.object(httpx, "AsyncClient", _Patched):
        from app.ee.oidc.graph_client import resolve_group_names
        result = await resolve_group_names("fake_token")

    assert "aaa-bbb-ccc" in result
    assert "role-123" not in result


@pytest.mark.asyncio
async def test_resolve_group_names_handles_pagination():
    """Graph API paginates — follows @odata.nextLink."""
    page1 = {
        "value": [
            {"@odata.type": "#microsoft.graph.group", "id": "g1", "displayName": "Group1"},
        ],
        "@odata.nextLink": "https://graph.microsoft.com/v1.0/me/memberOf?$skiptoken=abc",
    }
    page2 = {
        "value": [
            {"@odata.type": "#microsoft.graph.group", "id": "g2", "displayName": "Group2"},
        ],
    }

    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(200, json=page1)
        return httpx.Response(200, json=page2)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    with patch.object(httpx, "AsyncClient", _Patched):
        from app.ee.oidc.graph_client import resolve_group_names
        result = await resolve_group_names("fake_token")

    assert result == {"g1": "Group1", "g2": "Group2"}
    assert call_count == 2


@pytest.mark.asyncio
async def test_resolve_group_names_marks_missing_display_name_unresolved():
    """A group with no readable displayName maps to None, not to its own GUID."""
    graph_response = {
        "value": [
            {"@odata.type": "#microsoft.graph.group", "id": "g1", "displayName": "AllFabric"},
            {"@odata.type": "#microsoft.graph.group", "id": "g2"},
            {"@odata.type": "#microsoft.graph.group", "id": "g3", "displayName": "  "},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=graph_response)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    with patch.object(httpx, "AsyncClient", _Patched):
        from app.ee.oidc.graph_client import resolve_group_names
        result = await resolve_group_names("fake_token")

    # Membership is still known for all three — only the names differ.
    assert result == {"g1": "AllFabric", "g2": None, "g3": None}


@pytest.mark.asyncio
async def test_resolve_group_names_by_ids_leaves_unresolved_ids_none():
    """getByIds omits objects the app can't read; those must not be named by GUID."""
    resolved_id = "11111111-2222-3333-4444-555555555555"
    unreadable_id = "85f43b45-99ae-43a0-a780-a05c119e8b9c"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "app_token"})
        # Graph silently drops the object it cannot read.
        return httpx.Response(200, json={
            "value": [{"id": resolved_id, "displayName": "PowerBI-ServicePrincipals"}]
        })

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    with patch.object(httpx, "AsyncClient", _Patched):
        from app.ee.oidc.graph_client import resolve_group_names_by_ids
        result = await resolve_group_names_by_ids(
            group_ids=[resolved_id, unreadable_id],
            tenant_id="test-tenant",
            client_id="cid",
            client_secret="secret",
        )

    assert result == {resolved_id: "PowerBI-ServicePrincipals", unreadable_id: None}


@pytest.mark.asyncio
async def test_resolve_group_names_by_ids_asks_for_roles_and_units_too():
    """A directory role in the groups claim resolves instead of staying unnamed."""
    role_id = "85f43b45-99ae-43a0-a780-a05c119e8b9c"
    requested_types = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "app_token"})
        requested_types.append(json.loads(request.content)["types"])
        return httpx.Response(200, json={
            "value": [{
                "@odata.type": "#microsoft.graph.directoryRole",
                "id": role_id,
                "displayName": "Fabric Administrator",
            }]
        })

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    with patch.object(httpx, "AsyncClient", _Patched):
        from app.ee.oidc.graph_client import resolve_group_names_by_ids
        result = await resolve_group_names_by_ids(
            group_ids=[role_id], tenant_id="t", client_id="c", client_secret="s"
        )

    assert requested_types == [["group", "directoryRole", "administrativeUnit"]]
    assert result == {role_id: "Fabric Administrator"}


@pytest.mark.asyncio
async def test_resolve_group_names_by_ids_batches_beyond_fifteen():
    """More than one batch is issued, and every id appears in the result."""
    ids = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(32)]
    batches = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth2/v2.0/token"):
            return httpx.Response(200, json={"access_token": "app_token"})
        body = json.loads(request.content)
        batches.append(body["ids"])
        return httpx.Response(200, json={
            "value": [{"id": gid, "displayName": f"Group-{gid[:8]}"} for gid in body["ids"]]
        })

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    class _Patched(original):
        def __init__(self, *a, **kw):
            kw["transport"] = transport
            super().__init__(*a, **kw)

    with patch.object(httpx, "AsyncClient", _Patched):
        from app.ee.oidc.graph_client import resolve_group_names_by_ids
        result = await resolve_group_names_by_ids(
            group_ids=ids, tenant_id="t", client_id="c", client_secret="s"
        )

    assert [len(b) for b in batches] == [15, 15, 2]
    assert set(result) == set(ids)
    assert all(v is not None for v in result.values())


# ── Unresolved-name labelling ──


def test_unresolved_label_only_applies_to_guids():
    """A GUID gets the placeholder; a readable claim value is kept as-is."""
    from app.ee.oidc.group_sync_service import unresolved_group_label

    assert unresolved_group_label("85f43b45-99ae-43a0-a780-a05c119e8b9c") == (
        "Unresolved directory group (85f43b45…)"
    )
    assert unresolved_group_label("Engineering") == "Engineering"
    assert unresolved_group_label("CN=Sales,OU=Groups,DC=corp") == "CN=Sales,OU=Groups,DC=corp"


def test_resolved_name_rejects_id_shaped_names():
    """A name equal to the id is legacy backfill, not a resolution."""
    from app.ee.oidc.group_sync_service import _resolved_name

    gid = "85f43b45-99ae-43a0-a780-a05c119e8b9c"
    assert _resolved_name({gid: gid}, gid) is None
    assert _resolved_name({gid: None}, gid) is None
    assert _resolved_name({gid: "  "}, gid) is None
    assert _resolved_name({}, gid) is None
    assert _resolved_name({gid: "PowerBI-ServicePrincipals"}, gid) == "PowerBI-ServicePrincipals"


# ── ID token parsing tests ──


def test_extract_groups_from_id_token():
    """Groups claim is correctly read from decoded ID token."""
    import jwt as pyjwt

    claims = {
        "sub": "user1",
        "email": "user@test.com",
        "groups": ["group-aaa", "group-bbb"],
    }
    # Create a minimal unsigned JWT for testing
    id_token = pyjwt.encode(claims, "secret", algorithm="HS256")
    decoded = pyjwt.decode(id_token, options={"verify_signature": False})

    assert decoded.get("groups") == ["group-aaa", "group-bbb"]


def test_no_groups_claim_returns_empty():
    """If groups claim is absent, returns empty list."""
    import jwt as pyjwt

    claims = {"sub": "user1", "email": "user@test.com"}
    id_token = pyjwt.encode(claims, "secret", algorithm="HS256")
    decoded = pyjwt.decode(id_token, options={"verify_signature": False})

    assert decoded.get("groups", []) == []


def test_group_overage_detected():
    """When _claim_names present (overage), groups claim is absent."""
    import jwt as pyjwt

    claims = {
        "sub": "user1",
        "_claim_names": {"groups": "src1"},
        "_claim_sources": {
            "src1": {
                "endpoint": "https://graph.microsoft.com/v1.0/users/user1/getMemberObjects"
            }
        },
    }
    id_token = pyjwt.encode(claims, "secret", algorithm="HS256")
    decoded = pyjwt.decode(id_token, options={"verify_signature": False})

    # No groups claim, but _claim_names present → overage
    assert decoded.get("groups", []) == []
    assert "_claim_names" in decoded


def test_custom_group_claim_name():
    """Supports custom group claim names (e.g., 'roles' instead of 'groups')."""
    import jwt as pyjwt

    claims = {
        "sub": "user1",
        "custom_groups": ["grp-1", "grp-2"],
    }
    id_token = pyjwt.encode(claims, "secret", algorithm="HS256")
    decoded = pyjwt.decode(id_token, options={"verify_signature": False})

    group_claim = "custom_groups"
    assert decoded.get(group_claim, []) == ["grp-1", "grp-2"]
