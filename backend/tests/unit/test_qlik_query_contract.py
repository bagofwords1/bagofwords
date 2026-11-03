"""Exercise both clients through HTTP/WebSocket boundaries, not canned RPC replies.

The field selection contract is documented at:
https://help.qlik.com/en-US/sense-developer/May2025/Subsystems/EngineAPI/Content/Sense_EngineAPI/DiscoveringAndAnalysing/MakeSelections/select-values-in-field.htm
"""
import json
from contextlib import contextmanager

import pytest
import requests

from app.data_sources.clients.qlik_sense_client import QlikSenseClient
from tests.unit.test_qlik_sense_onprem_client import make_client


APP_ID = "11111111-2222-3333-4444-555555555555"
APPS = [
    {"id": APP_ID, "name": "Revenue", "published": True,
     "lastReloadTime": "2025-01-01",
     "stream": {"id": "sales", "name": "Sales"},
     "owner": {"userDirectory": "CORP", "userId": "owner"}},
    {"id": "22222222-2222-3333-4444-555555555555", "name": "Costs", "published": True,
     "lastReloadTime": "2026-01-01",
     "stream": {"id": "finance", "name": "Finance"}},
]


class Engine:
    def __init__(self, headers, selected=True, selected_fields=None):
        self.headers = dict(headers)
        self.selected = selected
        self.selection = None
        self.opened_app = None
        self.cube_created = False
        self.selected_fields = selected_fields
        self.field = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, raw):
        request = json.loads(raw)
        handle, method, params = request["handle"], request["method"], request["params"]
        self.reply = {"jsonrpc": "2.0", "id": request["id"]}
        if handle == -1 and method == "OpenDoc":
            self.opened_app = params[0]
            result = {"qReturn": {"qHandle": 10}}
        elif handle == 10 and method == "GetField":
            self.field = params[0]
            result = {"qReturn": {"qHandle": 20}}
        elif handle == 10 and method == "GetAppLayout":
            result = {"qLayout": {"qHasData": True}}
        elif handle == 10 and method == "GetTablesAndKeys":
            result = {"qtr": [], "qk": []}
        elif handle == 20 and method == "SelectValues":
            values, toggle, soft_lock = params
            assert toggle is False and soft_lock is False
            self.selection = values
            result = {"qReturn": self.selected}
        elif handle == 10 and method == "CreateSessionObject":
            if "qSelectionObjectDef" in params[0]:
                result = {"qReturn": {"qHandle": 40}}
            else:
                self.cube_created = True
                result = {"qReturn": {"qHandle": 30}}
        elif handle == 40 and method == "GetLayout":
            fields = self.selected_fields
            if fields is None:
                fields = [{"qField": self.field, "qSelectedCount": len(self.selection or [])}]
            result = {"qLayout": {"qSelectionObject": {"qSelections": fields}}}
        elif handle == 30 and method == "GetHyperCubeData":
            result = {"qDataPages": [{"qMatrix": [[{"qText": "matched"}]]}]}
        else:
            self.reply["error"] = {"code": -32601, "message": f"Unknown method {method} on handle {handle}"}
            return
        self.reply["result"] = result

    async def recv(self):
        return json.dumps(self.reply)


@pytest.fixture
def boundary(monkeypatch):
    sockets = []
    state = {"selected": True, "apps": APPS, "app_requests": 0}

    def get(session, url, **kwargs):
        if state.get("qrs_unavailable") and "/qrs/" in url:
            raise requests.Timeout("QRS temporarily unavailable")
        if url.endswith("/app/full") or url.endswith("/app"):
            state["app_requests"] += 1
            body = state["apps"]
        elif url.endswith("/stream/count"):
            body = {"value": 2}
        elif url.endswith("/stream/full"):
            body = []
        elif url.endswith("/about"):
            body = {"buildVersion": "test"}
        elif "/api/v1/items" in url:
            body = {"data": [], "links": {}}
        else:
            raise AssertionError(f"Unexpected HTTP request {url}")
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps(body).encode()
        return response

    def connect(uri, *, additional_headers, **kwargs):
        engine = Engine(additional_headers, state["selected"], state.get("selected_fields"))
        sockets.append(engine)
        return engine

    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr("websockets.connect", connect)
    return state, sockets


@contextmanager
def client_for(kind, **kwargs):
    client = (make_client(**kwargs) if kind == "onprem" else
              QlikSenseClient(base_url="https://tenant.qlikcloud.com", api_key="synthetic"))
    try:
        yield client
    finally:
        if kind == "onprem":
            client.close()


@pytest.mark.parametrize("kind", ["onprem", "cloud"])
@pytest.mark.parametrize("values,expected", [
    (["North", "001"], [{"qText": "North"}, {"qText": "001"}]),
    ([2026, 2.5], [{"qIsNumeric": True, "qNumber": 2026}, {"qIsNumeric": True, "qNumber": 2.5}]),
])
def test_filters_select_typed_values_on_a_field(kind, values, expected, boundary):
    _, sockets = boundary
    with client_for(kind) as client:
        frame = client.execute_query(app=APP_ID, dimensions=["Region"], filters={"Region": values}, max_rows=1)
    assert frame.iloc[0, 0] == "matched"
    assert sockets[0].selection == expected


@pytest.mark.parametrize("kind", ["onprem", "cloud"])
def test_rejected_selection_never_returns_an_unfiltered_cube(kind, boundary):
    state, sockets = boundary
    state["selected"] = False
    with client_for(kind) as client, pytest.raises(RuntimeError, match="selection.*Region"):
        client.execute_query(app=APP_ID, dimensions=["Region"], filters={"Region": ["missing"]})
    assert not sockets[0].cube_created


@pytest.mark.parametrize("kind", ["onprem", "cloud"])
@pytest.mark.parametrize("selected_fields", [[], [{"qField": "Order Date", "qSelectedCount": 0}]])
def test_successful_rpc_without_selected_values_never_builds_a_cube(kind, selected_fields, boundary):
    state, sockets = boundary
    # A dual date's display text need not match a text-only FieldValue.
    state["selected_fields"] = selected_fields
    with client_for(kind) as client, pytest.raises(RuntimeError, match="Order Date"):
        client.execute_query(app=APP_ID, dimensions=["Region"], filters={"Order Date": ["2026-01-01"]})
    assert not sockets[0].cube_created


@pytest.mark.parametrize("kind", ["onprem", "cloud"])
def test_verified_numeric_dual_date_can_be_selected(kind, boundary):
    state, sockets = boundary
    state["selected_fields"] = [{"qField": "Order Date", "qSelectedCount": 1}]
    with client_for(kind) as client:
        frame = client.execute_query(app=APP_ID, dimensions=["Region"], filters={"Order Date": [46023]})
    assert not frame.empty
    assert sockets[0].selection == [{"qIsNumeric": True, "qNumber": 46023}]


@pytest.mark.parametrize("kind", ["onprem", "cloud"])
def test_later_selection_cannot_silently_clear_an_earlier_filter(kind, boundary):
    state, sockets = boundary
    state["selected_fields"] = [{"qField": "Year", "qSelectedCount": 1}]
    with client_for(kind) as client, pytest.raises(RuntimeError, match="Region"):
        client.execute_query(app=APP_ID, dimensions=["Region"], filters={"Region": ["North"], "Year": [2025]})
    assert not sockets[0].cube_created


@pytest.mark.parametrize("kind", ["onprem", "cloud"])
@pytest.mark.parametrize("values", [[], [None], [True], [float("nan")], [float("inf")]])
def test_invalid_selection_values_do_not_silently_remove_the_filter(kind, values, boundary):
    _, sockets = boundary
    with client_for(kind) as client, pytest.raises(ValueError, match="Region"):
        client.execute_query(app=APP_ID, dimensions=["Region"], filters={"Region": values})
    assert not any(s.cube_created for s in sockets)


@pytest.mark.parametrize("identity,expected", [
    ({}, "UserDirectory=CORP; UserId=owner"),
    ({"user_directory": "CORP", "user_id": "member"}, "UserDirectory=CORP; UserId=member"),
])
def test_query_identity_is_independent_of_app_reference(identity, expected, boundary):
    _, sockets = boundary
    with client_for("onprem", **identity) as client:
        for target in (APP_ID, "Sales/Revenue", "Sales/Revenue/Orders", "Revenue"):
            client.execute_query(app=target, dimensions=["Region"], max_rows=1)
    assert {s.opened_app for s in sockets} == {APP_ID}
    assert {s.headers["X-Qlik-User"] for s in sockets} == {expected}


@pytest.mark.parametrize("token", [APP_ID, " Revenue ", " revenue "])
def test_app_filter_intersects_stream_filter_and_applies_before_cap(token, boundary):
    with client_for("onprem", app_filter=token, max_apps=1) as client:
        assert [app["id"] for app in client.list_apps()] == [APP_ID]
    with client_for("onprem", app_filter=token, stream_filter="Sales", max_apps=1) as client:
        assert [app["id"] for app in client.list_apps()] == [APP_ID]
        assert client.test_connection()["app_count"] == 1
    with client_for("onprem", app_filter=token, stream_filter="Finance") as client:
        assert client.list_apps() == []


def test_unknown_app_filter_never_expands_to_all_apps(boundary):
    with client_for("onprem", app_filter="missing") as client:
        assert client.get_schemas() == []
    assert boundary[1] == []


@pytest.mark.parametrize("app_filter", [None, " , ", "Revenue, Costs, Revenue"])
def test_empty_or_multiple_app_filters_preserve_matching_apps(app_filter, boundary):
    with client_for("onprem", app_filter=app_filter) as client:
        assert {app["name"] for app in client.list_apps()} == {"Revenue", "Costs"}


def test_app_id_filter_is_case_insensitive(boundary):
    state, _ = boundary
    app_id = "abcdefab-2222-3333-4444-555555555555"
    state["apps"] = [{**APPS[0], "id": app_id}]
    with client_for("onprem", app_filter=app_id.upper()) as client:
        assert [app["id"] for app in client.list_apps()] == [app_id]
        client.execute_query(app=app_id.upper() + "/Orders", dimensions=["Region"])
    assert boundary[1][0].opened_app == app_id


def test_empty_scope_connection_diagnostic_mentions_both_filters(boundary):
    with client_for("onprem", app_filter="missing") as client:
        result = client.test_connection()
    assert result["success"] and result["app_count"] == 0
    assert "app" in result["message"].lower() and "filter" in result["message"].lower()
    assert "app filter" in result["message"].lower()


def test_indexing_cap_does_not_restrict_queries_or_hide_ambiguity(boundary):
    state, sockets = boundary
    with client_for("onprem", max_apps=1) as client:
        assert [a["name"] for a in client.list_apps()] == ["Costs"]
        client.execute_query(app="Sales/Revenue/Orders", dimensions=["Region"])
    assert sockets[0].opened_app == APP_ID
    state["apps"] = [APPS[0], {**APPS[1], "name": "Revenue"}]
    with client_for("onprem", max_apps=1) as client, pytest.raises(ValueError, match="ambiguous"):
        client.execute_query(app="Revenue", dimensions=["Region"])


def test_query_metadata_is_cached_briefly_and_refreshed_without_identity_fallback(boundary, monkeypatch):
    state, sockets = boundary
    clock = [100.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    with client_for("onprem") as client:
        client.execute_query(app=APP_ID, dimensions=["Region"])
        state["qrs_unavailable"] = True
        client.execute_query(app="Sales/Revenue/Orders", dimensions=["Region"])
        assert state["app_requests"] == 1
        clock[0] += 31
        with pytest.raises(requests.Timeout):
            client.execute_query(app=APP_ID, dimensions=["Region"])
        state["qrs_unavailable"] = False
        state["apps"] = [{**APPS[0], "owner": {"userDirectory": "CORP", "userId": "new-owner"}}]
        client.execute_query(app=APP_ID, dimensions=["Region"])
    assert state["app_requests"] == 2
    assert sockets[-1].headers["X-Qlik-User"] == "UserDirectory=CORP; UserId=new-owner"


def test_app_filter_does_not_override_published_only(boundary):
    state, _ = boundary
    state["apps"] = [{**APPS[0], "published": False, "stream": {}}]
    with client_for("onprem", app_filter=APP_ID) as client:
        assert client.list_apps() == []
    with client_for("onprem", app_filter=APP_ID, published_only=False) as client:
        assert [app["id"] for app in client.list_apps()] == [APP_ID]


def test_only_selected_apps_are_opened_during_discovery(boundary):
    with client_for("onprem", app_filter=APP_ID, include_master_items=False, include_lineage=False) as client:
        client.get_schemas()
    assert [s.opened_app for s in boundary[1]] == [APP_ID]


def test_form_config_preserves_app_selection_and_serial_indexing(boundary):
    from app.schemas.data_sources.configs import QlikSenseOnPremConfig

    config = QlikSenseOnPremConfig(server_url="https://qlik.corp.example.com", app_filter=APP_ID, max_concurrency=1)
    with client_for("onprem", **config.model_dump()) as client:
        assert client.max_concurrency == 1
        assert [app["id"] for app in client.list_apps()] == [APP_ID]


@pytest.mark.parametrize("value", [0, -1, 5, 1.5, "", None])
def test_form_rejects_unbounded_indexing_concurrency(value):
    from pydantic import ValidationError
    from app.schemas.data_sources.configs import QlikSenseOnPremConfig

    with pytest.raises(ValidationError):
        QlikSenseOnPremConfig(server_url="https://qlik.corp.example.com", max_concurrency=value)


def test_unknown_query_target_never_opens_a_different_app(boundary):
    with client_for("onprem") as client, pytest.raises(ValueError, match="not found"):
        client.execute_query(app="Wrong/Revenue/Orders", dimensions=["Region"])
    assert boundary[1] == []


def test_listing_failure_is_not_retried_with_a_different_identity(boundary):
    state, sockets = boundary
    state["qrs_unavailable"] = True
    with client_for("onprem") as client, pytest.raises(requests.Timeout):
        client.execute_query(app=APP_ID, dimensions=["Region"])
    assert sockets == []


@pytest.mark.parametrize("second_stream", [{"name": "Finance"}, {}])
def test_ambiguous_bare_app_name_requires_qualification(second_stream, boundary):
    state, sockets = boundary
    state["apps"] = [APPS[0], {**APPS[1], "name": "Revenue", "stream": second_stream}]
    with client_for("onprem") as client, pytest.raises(ValueError, match="ambiguous"):
        client.execute_query(app="Revenue", dimensions=["Region"])
    assert sockets == []
