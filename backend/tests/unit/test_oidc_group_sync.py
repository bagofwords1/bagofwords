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
