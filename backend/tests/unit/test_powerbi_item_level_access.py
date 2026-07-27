"""Power BI: identities whose access to a model is item-level, not workspace-level.

Under row-level security an end user must NOT hold a workspace role — Admin,
Member and Contributor all BYPASS RLS, so only Viewer-or-less leaves RLS in
force, and the strictest (correct) setup grants Build on the semantic model
directly and no workspace role at all.

That topology breaks two assumptions the connector used to make, both verified
live against a real tenant (docs/feedback-loops/powerbi-obo-rls.md):

1. `GET /groups` returns only workspaces the identity holds a ROLE in, and
   `GET /myorg/datasets` is My-workspace-only — so an item-shared model appears
   in NEITHER listing and silently vanished from the user's catalog.
2. `POST /groups/{ws}/datasets/{ds}/executeQueries` answers 401 for such an
   identity, while the tenant-level `POST /datasets/{ds}/executeQueries`
   answers 200 for the very same user and query.

The fake below stands in for api.powerbi.com only; all client logic runs real.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.data_sources.clients.powerbi_client import PowerBIClient


def _resp(status: int, payload=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else {}
    r.headers = {}
    r.text = json.dumps(payload) if payload else ""
    return r


class _Tenant:
    """Routes by (method, url substring); longest match wins. Records calls."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self._routes: list[tuple[str, str, object]] = []

    def route(self, method, sub, response):
        self._routes.append((method.upper(), sub, response))

    def _dispatch(self, method, url):
        self.calls.append((method.upper(), url))
        matches = [(len(s), r) for m, s, r in self._routes if m == method.upper() and s in url]
        if matches:
            return max(matches, key=lambda x: x[0])[1]
        return _resp(404, {"error": {"code": "PowerBIEntityNotFound"}})

    def request(self, method, url, json=None, headers=None, timeout=None):
        return self._dispatch(method, url)

    def post(self, url, json=None, headers=None, timeout=None):
        return self._dispatch("POST", url)

    def get(self, url, headers=None, timeout=None):
        return self._dispatch("GET", url)

    def attempted(self, method, sub):
        return any(m == method.upper() and sub in u for m, u in self.calls)

    def count(self, method, sub):
        return sum(1 for m, u in self.calls if m == method.upper() and sub in u)


def _client(tenant):
    c = PowerBIClient(tenant_id="t", client_id="c", client_secret="s", access_token="tok")
    c._http = tenant
    return c


ROW_OK = _resp(200, {"results": [{"tables": [{"rows": [{"[t]": 1}]}]}]})

# Catalog the service principal indexed earlier: one model in a workspace the
# end user holds no role in.
PRIOR = {
    "shared_orders/Orders": {
        "columns": [{"name": "id", "dtype": "int64"}, {"name": "Region", "dtype": "string"}],
        "pks": [], "fks": [],
        "metadata_json": {"powerbi": {
            "datasetId": "ds-shared", "workspaceId": "ws-hidden",
            "workspaceName": "obo-itemshare", "datasetName": "shared_orders",
            "tableName": "Orders",
        }},
    }
}


def _no_listing(tenant):
    """The identity holds no workspace role: /groups is empty."""
    tenant.route("GET", "/groups", _resp(200, {"value": []}))


def test_item_shared_model_is_discovered_by_probe():
    """The model is in neither listing, but the identity CAN query it — it must
    end up in this identity's catalog."""
    t = _Tenant()
    _no_listing(t)
    t.route("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries", ROW_OK)

    tables = _client(t).get_schemas(prior_tables=PRIOR)

    assert [x.name for x in tables] == ["shared_orders/Orders"]
    assert [c.name for c in tables[0].columns] == ["id", "Region"]
    # Probed tenant-level: the workspace-scoped URL would 401 for this identity.
    assert t.attempted("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries")


def test_unqueryable_model_is_not_added_to_the_catalog():
    """A model the identity cannot query must stay out of their catalog — the
    probe is the authorization check, so a 404/401 means 'not yours'."""
    t = _Tenant()
    _no_listing(t)
    t.route("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries",
            _resp(404, {"error": {"code": "PowerBIEntityNotFound"}}))

    assert _client(t).get_schemas(prior_tables=PRIOR) == []


def test_rls_model_without_role_membership_is_not_added():
    """Build permission alone is not enough on an RLS model: Power BI answers
    401 RLSNotAuthorizedForModel until the user is in an RLS role. Treated the
    same as no access."""
    t = _Tenant()
    _no_listing(t)
    t.route("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries",
            _resp(401, {"error": {"code": "RLSNotAuthorizedForModel"}}))

    assert _client(t).get_schemas(prior_tables=PRIOR) == []


def test_listed_datasets_are_not_probed():
    """Datasets the workspace listing already returned must not cost an extra
    probe — executeQueries is rate-limited per user."""
    t = _Tenant()
    t.route("GET", "/groups", _resp(200, {"value": [{"id": "ws-hidden", "name": "obo-itemshare"}]}))
    t.route("GET", "/groups/ws-hidden/datasets",
            _resp(200, {"value": [{"id": "ds-shared", "name": "shared_orders"}]}))
    t.route("GET", "/groups/ws-hidden/reports", _resp(200, {"value": []}))

    tables = _client(t).get_schemas(prior_tables=PRIOR)

    assert [x.name for x in tables] == ["shared_orders/Orders"]
    assert t.count("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries") == 0


def test_probe_count_is_capped(monkeypatch):
    """A large catalog must not spend the user's whole executeQueries budget."""
    t = _Tenant()
    _no_listing(t)
    monkeypatch.setattr(PowerBIClient, "MAX_UNLISTED_PROBES", 3)
    prior = {
        f"m{i}/T": {
            "columns": [{"name": "id", "dtype": "int64"}], "pks": [], "fks": [],
            "metadata_json": {"powerbi": {"datasetId": f"ds{i}", "workspaceId": "ws-hidden",
                                          "datasetName": f"m{i}", "tableName": "T"}},
        }
        for i in range(10)
    }
    for i in range(10):
        t.route("POST", f"/v1.0/myorg/datasets/ds{i}/executeQueries", ROW_OK)

    tables = _client(t).get_schemas(prior_tables=prior)

    assert len(tables) == 3
    assert sum(t.count("POST", f"/v1.0/myorg/datasets/ds{i}/executeQueries") for i in range(10)) == 3


def test_query_falls_back_to_tenant_level_on_401():
    """The workspace-scoped endpoint 401s for an item-level identity; the query
    must still run via the tenant-level endpoint."""
    t = _Tenant()
    t.route("POST", "/groups/ws-hidden/datasets/ds-shared/executeQueries", _resp(401))
    t.route("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries",
            _resp(200, {"results": [{"tables": [{"rows": [{"Orders[Region]": "EMEA"}]}]}]}))

    c = _client(t)
    c.attach_table_metadata([{"name": n, "metadata_json": v["metadata_json"]} for n, v in PRIOR.items()])
    df = c.execute_query("EVALUATE Orders", "shared_orders/Orders")

    assert list(df["Region"]) == ["EMEA"]
    assert "ws-hidden" in c._tenant_scoped_workspaces


def test_tenant_level_fallback_is_sticky_per_workspace():
    """After one fallback the workspace-scoped URL is not retried again — the
    401 is a property of the identity, not of the query."""
    t = _Tenant()
    t.route("POST", "/groups/ws-hidden/datasets/ds-shared/executeQueries", _resp(401))
    t.route("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries",
            _resp(200, {"results": [{"tables": [{"rows": [{"Orders[Region]": "EMEA"}]}]}]}))

    c = _client(t)
    c.attach_table_metadata([{"name": n, "metadata_json": v["metadata_json"]} for n, v in PRIOR.items()])
    for _ in range(3):
        c.execute_query("EVALUATE Orders", "shared_orders/Orders")

    assert t.count("POST", "/groups/ws-hidden/datasets/ds-shared/executeQueries") == 1
    assert t.count("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries") == 3


def test_group_scoped_success_never_falls_back():
    """A workspace member keeps using the workspace-scoped endpoint."""
    t = _Tenant()
    t.route("POST", "/groups/ws-hidden/datasets/ds-shared/executeQueries",
            _resp(200, {"results": [{"tables": [{"rows": [{"Orders[Region]": "US"}]}]}]}))

    c = _client(t)
    c.attach_table_metadata([{"name": n, "metadata_json": v["metadata_json"]} for n, v in PRIOR.items()])
    c.execute_query("EVALUATE Orders", "shared_orders/Orders")

    assert c._tenant_scoped_workspaces == set()
    assert t.count("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries") == 0


def test_connect_succeeds_with_no_workspace_role_but_a_queryable_model():
    """The connect gate must not reject a user who holds no workspace role: a
    failed test deletes their credential, so this is the difference between
    'sees fewer tables' and 'cannot connect at all'."""
    t = _Tenant()
    _no_listing(t)
    t.route("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries", ROW_OK)

    c = _client(t)
    c.attach_table_metadata([{"name": n, "metadata_json": v["metadata_json"]} for n, v in PRIOR.items()])
    res = c.test_connection()

    assert res["success"] is True
    assert "shared_orders" in res["message"]


def test_connect_fails_cleanly_when_nothing_is_queryable():
    """No workspace role AND no reachable model is a real failure — but it must
    report connectivity so an admin can still save the connection."""
    t = _Tenant()
    _no_listing(t)
    t.route("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries", _resp(404))

    c = _client(t)
    c.attach_table_metadata([{"name": n, "metadata_json": v["metadata_json"]} for n, v in PRIOR.items()])
    res = c.test_connection()

    assert res["success"] is False
    assert res["connectivity"] is True
    assert "Build permission" in res["message"]


@pytest.mark.parametrize("code", [401, 403])
def test_forbidden_workspace_still_checks_the_known_catalog(code):
    """A user who can list a workspace but not query it may still hold Build on
    a model elsewhere; that must not be reported as a permission failure."""
    t = _Tenant()
    t.route("GET", "/groups", _resp(200, {"value": [{"id": "ws1", "name": "Sales"}]}))
    t.route("GET", "/groups/ws1/datasets", _resp(200, {"value": [{"id": "d1", "name": "Blocked"}]}))
    t.route("POST", "/groups/ws1/datasets/d1/executeQueries", _resp(code))
    t.route("POST", "/v1.0/myorg/datasets/ds-shared/executeQueries", ROW_OK)

    c = _client(t)
    c.attach_table_metadata([{"name": n, "metadata_json": v["metadata_json"]} for n, v in PRIOR.items()])
    res = c.test_connection()

    assert res["success"] is True
