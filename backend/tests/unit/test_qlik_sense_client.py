"""Unit tests for QlikSenseClient — all REST + QIX I/O is mocked."""

import json
import sys
from typing import List
from unittest.mock import MagicMock

import pytest

from app.data_sources.clients.qlik_sense_client import (
    QlikSenseClient,
    _build_hypercube_def,
    _hypercube_matrix_to_df,
    _tags_to_dtype,
)


# ---------------------------------------------------------------------------
# REST fixtures (shapes from qlik.dev REST API docs)
# ---------------------------------------------------------------------------

USERS_ME_OK = {
    "id": "user-1",
    "subject": "auth0|abc",
    "email": "svc@acme.com",
    "tenantId": "tenant-1",
}

ITEMS_APPS_PAGE_1 = {
    "data": [
        {
            "id": "item-1",
            "resourceId": "app-1",
            "name": "Pipeline 2025",
            "spaceId": "sp-sales",
            "spaceName": "Sales",
            "resourceType": "app",
            "updatedAt": "2026-03-01T12:00:00Z",
        },
        {
            "id": "item-2",
            "resourceId": "app-2",
            "name": "Forecast",
            "spaceId": "sp-fin",
            "spaceName": "Finance",
            "resourceType": "app",
            "updatedAt": "2026-03-02T12:00:00Z",
        },
    ],
    "links": {"next": {"href": "https://tenant.qlikcloud.com/api/v1/items?page=2"}},
}

ITEMS_APPS_PAGE_2 = {
    "data": [
        {
            "id": "item-3",
            "resourceId": "app-3",
            "name": "Ops",
            "spaceId": "sp-ops",
            "spaceName": "Operations",
            "resourceType": "app",
            "updatedAt": "2026-03-03T12:00:00Z",
        },
    ],
    "links": {},
}

ITEMS_APPS_SINGLE_PAGE = {
    "data": ITEMS_APPS_PAGE_1["data"],
    "links": {},
}

APP_METADATA_POPULATED = {
    "tables": [
        {"name": "Sales", "rows": 12345, "isLoose": False},
        {"name": "Customers", "rows": 420},
    ],
    "fields": [
        {"name": "OrderID", "srcTables": ["Sales", "Customers"], "tags": ["$key", "$integer"]},
        {"name": "Region", "srcTables": ["Sales"], "tags": ["$ascii", "$text"]},
        {"name": "Sales", "srcTables": ["Sales"], "tags": ["$numeric"]},
        {"name": "CustName", "srcTables": ["Customers"], "tags": ["$text"]},
    ],
}

APP_METADATA_EMPTY = {"tables": [], "fields": []}


# ---------------------------------------------------------------------------
# REST transport helpers
# ---------------------------------------------------------------------------

def _json_response(payload, status: int = 200):
    resp = MagicMock()
    resp.status_code = status
    resp.content = json.dumps(payload).encode("utf-8")
    resp.text = json.dumps(payload)
    resp.json = MagicMock(return_value=payload)
    return resp


def _install_rest(client: QlikSenseClient, url_to_payload, default=None):
    """
    Replace the client's HTTP session .get with a stub that routes by URL.
    `url_to_payload` is a dict of (path_or_full_url) -> payload (or callable).
    Missing URLs return `default` or raise AssertionError.
    """
    session = MagicMock()

    def _get(url, params=None, headers=None, timeout=None, verify=None):
        key = url
        match = url_to_payload.get(key)
        if match is None:
            # Try matching the suffix of url (everything after base_url)
            for candidate_key, payload in url_to_payload.items():
                if url.endswith(candidate_key):
                    match = payload
                    break
        if match is None:
            if default is not None:
                return _json_response(default)
            raise AssertionError(f"Unexpected GET {url} (params={params})")
        if callable(match):
            return _json_response(match(url, params))
        return _json_response(match)

    session.get.side_effect = _get
    client._http = session
    return session


# ---------------------------------------------------------------------------
# 1. Transport / auth
# ---------------------------------------------------------------------------

class TestTransport:
    def test_connect_requires_base_url(self):
        client = QlikSenseClient(base_url="", api_key="x")
        with pytest.raises(RuntimeError, match="base_url is required"):
            client.connect()

    def test_connect_requires_api_key(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key=None)
        with pytest.raises(RuntimeError, match="api_key is required"):
            client.connect()

    def test_rest_get_sets_bearer_header(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="secret-xyz")
        session = _install_rest(client, {"/api/v1/users/me": USERS_ME_OK})
        client._rest_get("/api/v1/users/me")
        _, kwargs = session.get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer secret-xyz"
        assert kwargs["headers"]["Accept"] == "application/json"

    def test_rest_get_propagates_http_errors(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        client.connect()
        err_resp = MagicMock(status_code=500, content=b"boom", text="boom")
        client._http = MagicMock()
        client._http.get.return_value = err_resp
        with pytest.raises(RuntimeError, match="HTTP 500"):
            client._rest_get("/api/v1/x")


# ---------------------------------------------------------------------------
# 2. test_connection
# ---------------------------------------------------------------------------

class TestTestConnection:
    def test_success(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        _install_rest(client, {
            "/api/v1/users/me": USERS_ME_OK,
            "/api/v1/items": ITEMS_APPS_SINGLE_PAGE,
        })
        result = client.test_connection()
        assert result["success"] is True
        assert result["has_apps"] is True
        assert "Connected to Qlik Cloud" in result["message"]

    def test_auth_failure(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        client.connect()
        err_resp = MagicMock(status_code=401, content=b"nope", text="nope")
        client._http = MagicMock()
        client._http.get.return_value = err_resp
        result = client.test_connection()
        assert result["success"] is False
        assert "Authentication failed" in result["message"]

    def test_empty_tenant(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        _install_rest(client, {
            "/api/v1/users/me": USERS_ME_OK,
            "/api/v1/items": {"data": [], "links": {}},
        })
        result = client.test_connection()
        assert result["success"] is True
        assert result["has_apps"] is False
        assert "no apps" in result["message"].lower()

    def test_test_connection_is_O1_in_tenant_size(self):
        """Must not iterate pages in test_connection (fast, bounded)."""
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        session = _install_rest(client, {
            "/api/v1/users/me": USERS_ME_OK,
            "/api/v1/items": ITEMS_APPS_SINGLE_PAGE,
        })
        client.test_connection()
        # Exactly 2 GETs: /users/me and /items (limit=1). No pagination follow.
        assert session.get.call_count == 2


# ---------------------------------------------------------------------------
# 3. list_apps — pagination + space filter
# ---------------------------------------------------------------------------

class TestListApps:
    def test_pagination_follows_links_next(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        _install_rest(client, {
            "/api/v1/items": ITEMS_APPS_PAGE_1,
            "https://tenant.qlikcloud.com/api/v1/items?page=2": ITEMS_APPS_PAGE_2,
        })
        apps = client.list_apps()
        names = [a["name"] for a in apps]
        assert names == ["Pipeline 2025", "Forecast", "Ops"]
        assert apps[0]["id"] == "app-1"
        assert apps[0]["space_name"] == "Sales"

    def test_space_filter_by_id(self):
        client = QlikSenseClient(
            base_url="https://tenant.qlikcloud.com",
            api_key="x",
            space_filter="sp-sales,sp-ops",
        )
        _install_rest(client, {
            "/api/v1/items": ITEMS_APPS_PAGE_1,
            "https://tenant.qlikcloud.com/api/v1/items?page=2": ITEMS_APPS_PAGE_2,
        })
        apps = client.list_apps()
        assert sorted(a["id"] for a in apps) == ["app-1", "app-3"]

    def test_space_filter_by_name(self):
        client = QlikSenseClient(
            base_url="https://tenant.qlikcloud.com",
            api_key="x",
            space_filter="Sales",
        )
        _install_rest(client, {
            "/api/v1/items": ITEMS_APPS_SINGLE_PAGE,
        })
        apps = client.list_apps()
        assert [a["id"] for a in apps] == ["app-1"]


# ---------------------------------------------------------------------------
# 4. get_schemas — REST fast path + QIX fallback + error isolation
# ---------------------------------------------------------------------------

class TestGetSchemas:
    def test_phase1_rest_only_builds_tables_and_keys(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x", max_concurrency=1)
        _install_rest(client, {
            "/api/v1/items": {"data": ITEMS_APPS_PAGE_1["data"][:1], "links": {}},
            "/api/v1/apps/app-1/data/metadata": APP_METADATA_POPULATED,
        })

        # Assert we don't fall through to QIX
        def _unreachable(*a, **kw):
            raise AssertionError("Phase 2 QIX should not be called when REST metadata is populated")
        client._qix_get_tables_and_keys = _unreachable

        tables = client.get_schemas()
        names = sorted(t.name for t in tables)
        assert names == ["Sales/Pipeline 2025/Customers", "Sales/Pipeline 2025/Sales"]

        sales = next(t for t in tables if t.name == "Sales/Pipeline 2025/Sales")
        col_names = sorted(c.name for c in sales.columns)
        assert col_names == ["OrderID", "Region", "Sales"]
        # $key → key dtype, included in pks
        pk_names = [c.name for c in sales.pks]
        assert pk_names == ["OrderID"]
        # FK from OrderID links Sales -> Customers (both carry the $key field)
        fk_targets = {fk.references_name for fk in sales.fks}
        assert "Sales/Pipeline 2025/Customers" in fk_targets

    def test_phase2_qix_fallback_when_rest_empty(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x", max_concurrency=1)
        _install_rest(client, {
            "/api/v1/items": {"data": ITEMS_APPS_PAGE_1["data"][:1], "links": {}},
            "/api/v1/apps/app-1/data/metadata": APP_METADATA_EMPTY,
        })

        qix_called = {"n": 0}

        def _qix(app_id):
            qix_called["n"] += 1
            qtr = [
                {"qName": "Sales", "qNoOfRows": 100, "qFields": [
                    {"qName": "OrderID", "qTags": ["$key", "$integer"]},
                    {"qName": "Region", "qTags": ["$text"]},
                ]},
                {"qName": "Customers", "qNoOfRows": 10, "qFields": [
                    {"qName": "OrderID", "qTags": ["$key", "$integer"]},
                    {"qName": "CustName", "qTags": ["$text"]},
                ]},
            ]
            qk = [{"qKeyFields": ["OrderID"], "qTables": ["Sales", "Customers"]}]
            return qtr, qk

        client._qix_get_tables_and_keys = _qix
        tables = client.get_schemas()
        assert qix_called["n"] == 1
        assert sorted(t.name for t in tables) == ["Sales/Pipeline 2025/Customers", "Sales/Pipeline 2025/Sales"]

        sales = next(t for t in tables if t.name == "Sales/Pipeline 2025/Sales")
        assert (sales.metadata_json or {}).get("qlik_sense", {}).get("source") == "qix"
        fk_targets = {fk.references_name for fk in sales.fks}
        assert "Sales/Pipeline 2025/Customers" in fk_targets

    def test_error_isolation_one_app_failure_does_not_abort(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x", max_concurrency=1)
        _install_rest(client, {
            "/api/v1/items": {"data": ITEMS_APPS_PAGE_1["data"], "links": {}},
            "/api/v1/apps/app-1/data/metadata": APP_METADATA_POPULATED,
            # app-2 metadata raises
        })
        # Make app-2 REST fetch raise
        original_rest = client._rest_get

        def _get_with_app2_blowup(path, params=None):
            if "app-2" in path:
                raise RuntimeError("simulated 403 on app-2")
            return original_rest(path, params=params)

        client._rest_get = _get_with_app2_blowup
        client._qix_get_tables_and_keys = lambda app_id: ([], [])
        tables = client.get_schemas()
        names = [t.name for t in tables]

        # app-1 fully materialized
        assert "Sales/Pipeline 2025/Sales" in names
        assert "Sales/Pipeline 2025/Customers" in names
        # app-2 stubbed out with an error entry, not dropped silently
        stubs = [t for t in tables if (t.metadata_json or {}).get("qlik_sense", {}).get("status") == "error"]
        assert len(stubs) == 1
        assert stubs[0].is_active is False

    def test_get_schema_by_full_name(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x", max_concurrency=1)
        _install_rest(client, {
            "/api/v1/items": {"data": ITEMS_APPS_PAGE_1["data"][:1], "links": {}},
            "/api/v1/apps/app-1/data/metadata": APP_METADATA_POPULATED,
        })
        tbl = client.get_schema("Sales/Pipeline 2025/Customers")
        assert [c.name for c in tbl.columns] == ["OrderID", "CustName"]

    def test_get_schema_not_found(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        _install_rest(client, {"/api/v1/items": {"data": [], "links": {}}})
        with pytest.raises(RuntimeError, match="Table not found"):
            client.get_schema("Missing/App/Table")


# ---------------------------------------------------------------------------
# 5. Hypercube helpers
# ---------------------------------------------------------------------------

class TestHypercubeBuilder:
    def test_build_dimensions_and_measures(self):
        defn = _build_hypercube_def(
            dimensions=["Region"],
            measures=[{"expr": "Sum([Sales])", "alias": "Total Sales"}],
            max_rows=1000,
        )
        assert defn["qDimensions"][0]["qDef"]["qFieldDefs"] == ["Region"]
        assert defn["qMeasures"][0]["qDef"]["qDef"] == "Sum([Sales])"
        assert defn["qMeasures"][0]["qDef"]["qLabel"] == "Total Sales"
        assert defn["qInitialDataFetch"][0]["qWidth"] == 2

    def test_matrix_to_df_uses_numeric_for_measures(self):
        defn = _build_hypercube_def(
            dimensions=["Region"],
            measures=[{"expr": "Sum([Sales])", "alias": "Total"}],
            max_rows=1000,
        )
        matrix = [
            [{"qText": "EMEA", "qNum": "NaN"}, {"qText": "$123,000", "qNum": 123000}],
            [{"qText": "APAC", "qNum": "NaN"}, {"qText": "$99,500", "qNum": 99500}],
        ]
        df = _hypercube_matrix_to_df(defn, matrix)
        assert list(df.columns) == ["Region", "Total"]
        assert df.iloc[0]["Region"] == "EMEA"
        assert df.iloc[0]["Total"] == 123000
        assert df.iloc[1]["Total"] == 99500

    def test_matrix_to_df_handles_empty(self):
        defn = _build_hypercube_def(
            dimensions=["X"], measures=[], max_rows=10,
        )
        df = _hypercube_matrix_to_df(defn, [])
        assert list(df.columns) == ["X"]
        assert len(df) == 0


# ---------------------------------------------------------------------------
# 6. execute_query — QIX session driven by a fake WebSocket
# ---------------------------------------------------------------------------

class _FakeWebSocket:
    """Replays canned JSON-RPC responses in order and records sent messages."""

    def __init__(self, canned: List[dict]):
        self._canned = list(canned)
        self.sent: List[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def send(self, raw: str):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._canned:
            raise AssertionError("FakeWebSocket ran out of canned responses")
        return json.dumps(self._canned.pop(0))


def _fake_connect(canned: List[dict]):
    fake = _FakeWebSocket(canned)

    def _connect(*args, **kwargs):
        return fake

    return _connect, fake


class TestExecuteQuery:
    def test_build_and_fetch_happy_path(self, monkeypatch):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        client.connect()

        canned = [
            # OpenDoc
            {"jsonrpc": "2.0", "id": 1, "result": {"qReturn": {"qHandle": 10}}},
            # CreateSessionObject
            {"jsonrpc": "2.0", "id": 2, "result": {"qReturn": {"qHandle": 42}}},
            # GetHyperCubeData — 2 rows, done
            {"jsonrpc": "2.0", "id": 3, "result": {"qDataPages": [{
                "qMatrix": [
                    [{"qText": "EMEA", "qNum": "NaN"}, {"qText": "$123,000", "qNum": 123000}],
                    [{"qText": "APAC", "qNum": "NaN"}, {"qText": "$99,500", "qNum": 99500}],
                ],
            }]}},
        ]
        connect_fn, fake = _fake_connect(canned)
        fake_websockets = MagicMock()
        fake_websockets.connect = connect_fn

        # Only list_apps runs against REST; give it a trivial map so appId resolves.
        _install_rest(client, {
            "/api/v1/items": {"data": [{
                "id": "item-1", "resourceId": "app-1", "name": "Pipeline 2025",
                "spaceId": "sp-sales", "spaceName": "Sales", "resourceType": "app",
            }], "links": {}},
        })

        monkeypatch.setitem(sys.modules, "websockets", fake_websockets)

        df = client.execute_query(
            app="Sales/Pipeline 2025",
            dimensions=["Region"],
            measures=[{"expr": "Sum([Sales])", "alias": "Total Sales"}],
            max_rows=100,
        )
        assert list(df.columns) == ["Region", "Total Sales"]
        assert df.iloc[0]["Total Sales"] == 123000

        methods = [m["method"] for m in fake.sent]
        assert methods == ["OpenDoc", "CreateSessionObject", "GetHyperCubeData"]
        cube_def = fake.sent[1]["params"][0]["qHyperCubeDef"]
        assert cube_def["qDimensions"][0]["qDef"]["qFieldDefs"] == ["Region"]
        assert cube_def["qMeasures"][0]["qDef"]["qDef"] == "Sum([Sales])"

    def test_filters_applied_as_selections(self, monkeypatch):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        client.connect()
        # _resolve_app_id now always attempts a list_apps() lookup first; give
        # it an empty result so an opaque ID passes straight through.
        _install_rest(client, {"/api/v1/items": {"data": [], "links": {}}})
        canned = [
            {"jsonrpc": "2.0", "id": 1, "result": {"qReturn": {"qHandle": 10}}},
            {"jsonrpc": "2.0", "id": 2, "result": {}},  # SelectInField
            {"jsonrpc": "2.0", "id": 3, "result": {"qReturn": {"qHandle": 42}}},
            {"jsonrpc": "2.0", "id": 4, "result": {"qDataPages": [{"qMatrix": []}]}},
        ]
        connect_fn, fake = _fake_connect(canned)
        fake_websockets = MagicMock()
        fake_websockets.connect = connect_fn
        monkeypatch.setitem(sys.modules, "websockets", fake_websockets)

        df = client.execute_query(
            app="app-1",
            dimensions=["Product"],
            measures=[{"expr": "Sum([Revenue])", "alias": "Rev"}],
            filters={"Region": ["EMEA"]},
            max_rows=10,
        )
        assert df.empty
        methods = [m["method"] for m in fake.sent]
        # Selections must come BEFORE CreateSessionObject
        assert methods == ["OpenDoc", "SelectInField", "CreateSessionObject", "GetHyperCubeData"]
        select_call = fake.sent[1]
        assert select_call["params"][0] == "Region"
        values = select_call["params"][1]["qValues"]
        assert [v["qText"] for v in values] == ["EMEA"]

    def test_requires_app(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        with pytest.raises(ValueError, match="app"):
            client.execute_query(dimensions=["X"])

    def test_requires_dim_or_measure(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        with pytest.raises(ValueError, match="dimension or measure"):
            client.execute_query(app="app-1")


# ---------------------------------------------------------------------------
# 7. Prompt / description
# ---------------------------------------------------------------------------

class TestDescription:
    def test_description_mentions_hypercube(self):
        client = QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="x")
        text = client.description
        assert "Qlik" in text
        assert "hypercube" in text.lower()
        # The three canonical examples are present
        assert "Sum([Sales])" in text
        assert "filters=" in text
        assert "Count([OrderID])" in text


# ---------------------------------------------------------------------------
# 8. Helper: _tags_to_dtype
# ---------------------------------------------------------------------------

class TestTagsToDtype:
    @pytest.mark.parametrize("tags,expected", [
        (["$numeric"], "numeric"),
        (["$key", "$integer"], "key"),  # $key dominates
        (["$text"], "text"),
        (["$ascii"], "text"),
        (["$date"], "date"),
        (["$timestamp"], "timestamp"),
        ([], "unknown"),
        (None, "unknown"),
    ])
    def test_tag_priority(self, tags, expected):
        assert _tags_to_dtype(tags) == expected
