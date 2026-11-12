"""The prompt-box file picker's Drive/OneDrive search endpoint.

GET /data_sources/{id}/connections/{cid}/files/search runs the connector's own
provider-side search (`search_files`) so the picker can match a filename as the
user types. These tests pin the contract the picker depends on: matches come
back shaped and capped to the requested limit, a blank query is a no-op that
never touches the source, a source that can't search says so instead of
erroring, and a per-user connection the caller hasn't linked returns the
structured connect-required state (not a 500).

The route and the access gate are real; only the remote each client talks to is
simulated — the same boundary (external Drive/Graph driver) the browse tests
mock. The agent the connection hangs off is an ordinary network_dir one; only
client construction is swapped.
"""
import pytest
from fastapi import HTTPException

from app.data_sources.clients.base import Capability
from tests.e2e.test_connection_file_browse import _agent_on_dir, _serve


class _SearchableClient:
    """A file client that supports provider-side search. Mirrors the real
    providers: `asearch_files` returns the files whose name contains the query
    (the providers also match content, but the backend trusts whatever the
    provider returns — it does no post-filtering)."""

    capabilities = {Capability.SEARCH_FILES, Capability.LIST_FILES, Capability.READ_FILE}

    def __init__(self, files):
        self._files = files

    async def asearch_files(self, query, **kwargs):
        ql = query.lower()
        return [f for f in self._files if ql in f["name"].lower()]


def _file(name, **over):
    f = {"id": f"id-{name}", "name": name, "mime_type": "text/csv",
         "size": 10, "modified_at": "2026-01-01T00:00:00Z",
         "web_url": f"https://drive/{name}"}
    f.update(over)
    return f


@pytest.fixture
def search_endpoint(tmp_path, test_client, create_user, login_user, whoami, create_data_source):
    listing, headers = _agent_on_dir(tmp_path, create_user, login_user, whoami, create_data_source, test_client)
    return listing + "/search", headers


@pytest.mark.e2e
def test_search_returns_only_matches_shaped_for_the_picker(search_endpoint, test_client, monkeypatch):
    endpoint, headers = search_endpoint
    files = [_file("budget.xlsx", mime_type="application/vnd.ms-excel"),
             _file("q1_budget.csv"), _file("sales.csv")]
    _serve(monkeypatch, lambda: _SearchableClient(files))

    body = test_client.get(endpoint, params={"q": "budget"}, headers=headers).json()
    names = sorted(f["name"] for f in body["files"])
    assert names == ["budget.xlsx", "q1_budget.csv"]
    assert body["total"] == 2 and body["has_more"] is False
    assert body["query"] == "budget"
    # Every result carries the fields the picker renders a chip from.
    for f in body["files"]:
        assert set(f) >= {"id", "name", "size", "mime_type", "web_url"}
        assert f["id"] and f["name"]


@pytest.mark.e2e
def test_search_caps_results_at_the_requested_limit(search_endpoint, test_client, monkeypatch):
    endpoint, headers = search_endpoint
    many = [_file(f"report_{i:02d}.csv") for i in range(30)]
    _serve(monkeypatch, lambda: _SearchableClient(many))

    body = test_client.get(endpoint, params={"q": "report", "limit": 10}, headers=headers).json()
    assert len(body["files"]) == 10
    assert body["total"] == 30 and body["has_more"] is True
    assert body["limit"] == 10

    # The default limit bounds the page even when the caller omits it.
    body = test_client.get(endpoint, params={"q": "report"}, headers=headers).json()
    assert len(body["files"]) == body["limit"] and body["limit"] <= 30


@pytest.mark.e2e
def test_blank_query_is_a_noop_that_never_touches_the_source(search_endpoint, test_client, monkeypatch):
    endpoint, headers = search_endpoint

    class _Exploding(_SearchableClient):
        async def asearch_files(self, query, **kwargs):
            raise AssertionError("a blank query must not reach the provider")

    _serve(monkeypatch, lambda: _Exploding([]))
    for blank in ("", "   "):
        body = test_client.get(endpoint, params={"q": blank}, headers=headers).json()
        assert body["files"] == [] and body["total"] == 0


@pytest.mark.e2e
def test_source_without_search_capability_reports_it_instead_of_erroring(search_endpoint, test_client, monkeypatch):
    endpoint, headers = search_endpoint

    class _NoSearch:
        capabilities = {Capability.LIST_FILES, Capability.READ_FILE}

    _serve(monkeypatch, lambda: _NoSearch())
    r = test_client.get(endpoint, params={"q": "anything"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["search_supported"] is False
    assert body["files"] == []


@pytest.mark.e2e
def test_unlinked_per_user_connection_returns_connect_required(search_endpoint, test_client, monkeypatch):
    """A per-user Drive the caller hasn't signed in to raises a 403 'connect'
    from construct_client — the picker must get the structured connect-required
    state so it can show a Connect button, not a generic error."""
    endpoint, headers = search_endpoint
    from app.services.connection_service import ConnectionService

    async def not_connected(self, *args, **kwargs):
        raise HTTPException(status_code=403, detail="Connect your account to use this connection")
    monkeypatch.setattr(ConnectionService, "construct_client", not_connected)

    r = test_client.get(endpoint, params={"q": "budget"}, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["connect_required"] is True
    assert body["files"] == []


@pytest.mark.e2e
def test_a_client_that_cannot_be_opened_is_a_400_not_a_500(search_endpoint, test_client, monkeypatch):
    endpoint, headers = search_endpoint
    from app.services.connection_service import ConnectionService

    async def broken(self, *args, **kwargs):
        raise ValueError("saved config no longer matches the client")
    monkeypatch.setattr(ConnectionService, "construct_client", broken)
    r = test_client.get(endpoint, params={"q": "budget"}, headers=headers)
    assert r.status_code == 400, r.text


@pytest.mark.e2e
def test_search_requires_authentication(search_endpoint, test_client, monkeypatch):
    """Search sits behind the same `view`-on-data-source gate as browse (the
    shared `requires_resource_permission` decorator, whose role matrix is
    covered in rbac/). The reachable boundary from here is auth itself: an
    unauthenticated caller never reaches the provider."""
    endpoint, _ = search_endpoint

    class _Exploding(_SearchableClient):
        async def asearch_files(self, query, **kwargs):
            raise AssertionError("an unauthenticated caller must not reach the provider")

    _serve(monkeypatch, lambda: _Exploding([]))
    r = test_client.get(endpoint, params={"q": "budget"})
    assert r.status_code in (401, 403), r.text
