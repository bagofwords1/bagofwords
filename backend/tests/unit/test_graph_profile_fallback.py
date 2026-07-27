"""Unit tests for graph_client.resolve_user_profile — the /me $select fetch and
its all-or-nothing fallback. No network: httpx.AsyncClient is faked.

Graph evaluates $select all-or-nothing (one restricted field 403s the whole
request). resolve_user_profile must fall back to the default projection so the
readable fields still sync instead of losing everything.
"""
import httpx
import pytest

from app.ee.oidc import graph_client


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=None)


class _FakeClient:
    """Returns queued responses in order across `get` calls."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.urls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        self.urls.append(url)
        return self._responses.pop(0)


def _patch(monkeypatch, responses):
    holder = {}
    def factory(*a, **k):
        client = _FakeClient(responses)
        holder["client"] = client
        return client
    monkeypatch.setattr(graph_client.httpx, "AsyncClient", factory)
    return holder


@pytest.mark.asyncio
async def test_happy_path_returns_selected_fields(monkeypatch):
    _patch(monkeypatch, [_FakeResp(200, {"jobTitle": "Analyst", "department": "Finance"})])
    out = await graph_client.resolve_user_profile("tok", ["jobTitle", "department"])
    assert out == {"jobTitle": "Analyst", "department": "Finance"}


@pytest.mark.asyncio
async def test_unset_fields_come_back_none(monkeypatch):
    _patch(monkeypatch, [_FakeResp(200, {"jobTitle": "Analyst"})])
    out = await graph_client.resolve_user_profile("tok", ["jobTitle", "department"])
    assert out == {"jobTitle": "Analyst", "department": None}


@pytest.mark.asyncio
async def test_403_falls_back_to_default_projection(monkeypatch):
    # First call ($select) 403s; fallback (no $select) returns the default set,
    # which lacks department — so only the readable field survives.
    holder = _patch(monkeypatch, [
        _FakeResp(403, {"error": {"code": "Authorization_RequestDenied"}}),
        _FakeResp(200, {"jobTitle": "Analyst", "officeLocation": "HQ"}),
    ])
    out = await graph_client.resolve_user_profile("tok", ["jobTitle", "department"])
    assert out == {"jobTitle": "Analyst"}          # department absent, not fatal
    assert "$select" in holder["client"].urls[0]   # first attempt used $select
    assert "$select" not in holder["client"].urls[1]  # fallback dropped it


@pytest.mark.asyncio
async def test_400_also_triggers_fallback(monkeypatch):
    _patch(monkeypatch, [
        _FakeResp(400, {"error": {"code": "Request_BadRequest"}}),
        _FakeResp(200, {"jobTitle": "Analyst"}),
    ])
    out = await graph_client.resolve_user_profile("tok", ["jobTitle"])
    assert out == {"jobTitle": "Analyst"}


@pytest.mark.asyncio
async def test_empty_fields_short_circuits(monkeypatch):
    _patch(monkeypatch, [])  # no HTTP call should happen
    out = await graph_client.resolve_user_profile("tok", [])
    assert out == {}
