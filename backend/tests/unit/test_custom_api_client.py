"""Unit tests for CustomApiClient.

test_connection: a reachable host that answers with an error status (404 wrong
base URL, 401/403 bad credentials, 5xx) must NOT be reported as a
connected/success — otherwise the UI shows a green "Connected (HTTP 404)" for
a broken connection.

Plus: auth header construction (incl. HTTP Basic), tool-schema generation
(metadata, complex parameter types), request routing (path/query/body/header
params), oversized-response parse skipping, error-body extraction, and binary
response handling.
"""

import base64
import json as jsonlib
from unittest.mock import MagicMock, patch

import pytest

from app.data_sources.clients.custom_api_client import (
    CustomApiClient,
    MAX_PARSE_CHARS,
)


@pytest.mark.parametrize(
    "status,expected_success,expected_reachable",
    [
        (200, True, True),
        (204, True, True),
        (302, True, True),
        (405, True, True),   # HEAD not allowed on base path, but host/path is reachable
        # 4xx: the probe failed but the HOST answered — success stays False
        # (the Test button warns honestly) while `reachable` stays True so
        # saving a connection to a root-404 API is not hard-blocked.
        (400, False, True),
        (401, False, True),
        (403, False, True),
        (404, False, True),
        (500, False, False),
    ],
)
def test_test_connection_status(status, expected_success, expected_reachable):
    client = CustomApiClient(base_url="https://api.example.com/v1")
    with patch("httpx.Client") as MockClient:
        inst = MockClient.return_value.__enter__.return_value
        inst.head.return_value = MagicMock(status_code=status)
        result = client.test_connection()
    assert result["success"] is expected_success
    assert result["reachable"] is expected_reachable
    assert str(status) in result["message"]


def test_test_connection_network_error_not_reachable():
    client = CustomApiClient(base_url="https://nope.invalid")
    with patch("httpx.Client") as MockClient:
        inst = MockClient.return_value.__enter__.return_value
        inst.head.side_effect = RuntimeError("name resolution failed")
        result = client.test_connection()
    assert result["reachable"] is False


def test_test_connection_network_error():
    client = CustomApiClient(base_url="https://nope.invalid")
    with patch("httpx.Client") as MockClient:
        inst = MockClient.return_value.__enter__.return_value
        inst.head.side_effect = RuntimeError("name resolution failed")
        result = client.test_connection()
    assert result["success"] is False
    assert "Failed to connect" in result["message"]


# ── Auth headers ───────────────────────────────────────────────────────────

def test_basic_auth_header():
    client = CustomApiClient(
        base_url="https://api.example.com",
        auth_type="basic", username="alice", password="s3cret",
    )
    headers = client._build_headers()
    expected = base64.b64encode(b"alice:s3cret").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"


def test_basic_auth_header_empty_password():
    client = CustomApiClient(
        base_url="https://api.example.com",
        auth_type="basic", username="alice",
    )
    headers = client._build_headers()
    expected = base64.b64encode(b"alice:").decode("ascii")
    assert headers["Authorization"] == f"Basic {expected}"


def test_bearer_auth_header_unchanged():
    client = CustomApiClient(
        base_url="https://api.example.com", auth_type="bearer", token="tok",
    )
    assert client._build_headers()["Authorization"] == "Bearer tok"


def test_no_auth_no_authorization_header():
    client = CustomApiClient(base_url="https://api.example.com")
    assert "Authorization" not in client._build_headers()


# ── list_tools: schema + metadata ─────────────────────────────────────────

_COMPLEX_ENDPOINTS = [
    {
        "name": "search_orders",
        "method": "POST",
        "path": "/orders/search",
        "description": "Search orders",
        "parameters": [
            {"name": "status", "in": "query", "type": "string",
             "enum": ["open", "closed"], "default": "open"},
            {"name": "filters", "in": "body", "type": "object",
             "properties": {"min_total": {"type": "number"}},
             "description": "Structured filter object"},
            {"name": "tags", "in": "body", "type": "array",
             "items": {"type": "string"}, "required": True},
            {"name": "X-Request-Id", "in": "header", "type": "string"},
        ],
    },
    {
        "name": "get_order",
        "method": "GET",
        "path": "/orders/{order_id}",
        "parameters": [
            {"name": "order_id", "in": "path", "type": "string", "required": True},
        ],
    },
]


def test_list_tools_emits_http_metadata():
    client = CustomApiClient(base_url="https://api.example.com", endpoints=_COMPLEX_ENDPOINTS)
    tools = {t["name"]: t for t in client.list_tools()}
    assert tools["search_orders"]["metadata"] == {"method": "POST", "path": "/orders/search"}
    assert tools["get_order"]["metadata"] == {"method": "GET", "path": "/orders/{order_id}"}
    # Write endpoints default to "ask"; reads carry no default_policy.
    assert tools["search_orders"]["default_policy"] == "ask"
    assert "default_policy" not in tools["get_order"]


def test_list_tools_complex_param_schema():
    client = CustomApiClient(base_url="https://api.example.com", endpoints=_COMPLEX_ENDPOINTS)
    schema = client.list_tools()[0]["input_schema"]
    props = schema["properties"]
    assert props["status"]["enum"] == ["open", "closed"]
    assert props["status"]["default"] == "open"
    assert props["filters"]["type"] == "object"
    assert props["filters"]["properties"]["min_total"]["type"] == "number"
    assert props["tags"]["type"] == "array"
    assert props["tags"]["items"] == {"type": "string"}
    assert schema["required"] == ["tags"]


def test_list_tools_param_schema_override():
    endpoints = [{
        "name": "create_thing", "method": "POST", "path": "/things",
        "parameters": [
            {"name": "payload", "in": "body",
             "schema": {"type": "object", "properties": {"deep": {"type": "array"}}},
             "description": "raw schema wins"},
        ],
    }]
    client = CustomApiClient(base_url="https://api.example.com", endpoints=endpoints)
    prop = client.list_tools()[0]["input_schema"]["properties"]["payload"]
    assert prop["properties"] == {"deep": {"type": "array"}}
    assert prop["description"] == "raw schema wins"


def test_list_tools_unknown_type_falls_back_to_string():
    endpoints = [{
        "name": "x", "method": "GET", "path": "/x",
        "parameters": [{"name": "p", "type": "banana"}],
    }]
    client = CustomApiClient(base_url="https://api.example.com", endpoints=endpoints)
    assert client.list_tools()[0]["input_schema"]["properties"]["p"]["type"] == "string"


# ── call_tool: request routing ─────────────────────────────────────────────

def _mock_response(status=200, json_data=None, text=None, content_type="application/json", content=b""):
    resp = MagicMock()
    resp.status_code = status
    if json_data is not None:
        text = jsonlib.dumps(json_data)
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = jsonlib.JSONDecodeError("x", "y", 0)
    resp.text = text if text is not None else ""
    resp.content = content
    resp.headers = {"content-type": content_type}
    resp.request = MagicMock()
    resp.request.url = "https://api.example.com/called"
    return resp


def _call(client, tool, args, response):
    with patch("httpx.Client") as MockClient:
        inst = MockClient.return_value.__enter__.return_value
        inst.request.return_value = response
        result = client.call_tool(tool, args)
    return result, inst.request


def test_call_tool_routes_path_query_body_header_params():
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[{
        "name": "update_order", "method": "PUT", "path": "/orders/{order_id}",
        "parameters": [
            {"name": "order_id", "in": "path", "type": "string", "required": True},
            {"name": "notify", "in": "query", "type": "boolean"},
            {"name": "fields", "in": "body", "type": "object"},
            {"name": "X-Trace", "in": "header", "type": "string"},
        ],
    }])
    result, request = _call(
        client, "update_order",
        {"order_id": "42", "notify": True, "fields": {"status": "closed"}, "X-Trace": "t1"},
        _mock_response(json_data={"ok": True}),
    )
    assert result["success"] is True
    args, kwargs = request.call_args
    assert args[0] == "PUT"
    assert args[1] == "https://api.example.com/orders/42"
    assert kwargs["params"] == {"notify": True}
    assert kwargs["json"] == {"fields": {"status": "closed"}}
    assert kwargs["headers"]["X-Trace"] == "t1"


def test_call_tool_undeclared_args_go_to_body_for_post():
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[{
        "name": "create", "method": "POST", "path": "/things", "parameters": [],
    }])
    result, request = _call(
        client, "create", {"a": 1, "b": {"c": 2}}, _mock_response(json_data={"id": 9}),
    )
    assert result["success"] is True
    assert request.call_args.kwargs["json"] == {"a": 1, "b": {"c": 2}}


def test_call_tool_undeclared_args_go_to_query_for_get():
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[{
        "name": "list", "method": "GET", "path": "/things", "parameters": [],
    }])
    result, request = _call(client, "list", {"page": 2}, _mock_response(json_data=[]))
    assert result["success"] is True
    assert request.call_args.kwargs["params"] == {"page": 2}
    assert "json" not in request.call_args.kwargs


def test_call_tool_unknown_endpoint_lists_known_names():
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[
        {"name": "real_tool", "method": "GET", "path": "/x"},
    ])
    result = client.call_tool("nope", {})
    assert result["success"] is False
    assert "real_tool" in result["error"]


# ── call_tool: response handling ───────────────────────────────────────────

def test_call_tool_tabular_detection():
    rows = [{"id": 1}, {"id": 2}]
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[
        {"name": "rows", "method": "GET", "path": "/rows"},
    ])
    result, _ = _call(client, "rows", {}, _mock_response(json_data=rows))
    assert result["success"] is True
    assert result["content_type"] == "tabular"
    assert result["data"] == rows


def test_call_tool_json_with_text_content_type_still_parses():
    payload = {"a": [1, 2]}
    resp = _mock_response(text=jsonlib.dumps(payload), content_type="text/plain")
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[
        {"name": "t", "method": "GET", "path": "/t"},
    ])
    result, _ = _call(client, "t", {}, resp)
    assert result["data"] == payload


def test_call_tool_oversized_json_skips_parse():
    big = "[" + ",".join('{"i":%d}' % i for i in range(10)) + "]"
    big = big + " " * (MAX_PARSE_CHARS + 1 - len(big))  # pad past the cap
    resp = _mock_response(text=big, content_type="application/json")
    resp.json.side_effect = AssertionError("must not parse oversized body")
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[
        {"name": "big", "method": "GET", "path": "/big"},
    ])
    result, _ = _call(client, "big", {}, resp)
    assert result["success"] is True
    assert result["parse_skipped"] is True
    assert result["size_chars"] == len(big)
    assert result["data"] == big


def test_call_tool_error_message_extracted_from_json_body():
    resp = _mock_response(
        status=422,
        json_data={"detail": [{"msg": "field required", "loc": ["body", "name"]}]},
    )
    # _extract_error_message digs into dict/list shapes for a usable string.
    resp.json.return_value = {"error": {"message": "order_id must be a UUID"}}
    resp.text = '{"error": {"message": "order_id must be a UUID"}}'
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[
        {"name": "t", "method": "GET", "path": "/t"},
    ])
    result, _ = _call(client, "t", {}, resp)
    assert result["success"] is False
    assert "HTTP 422" in result["error"]
    assert "order_id must be a UUID" in result["error"]


def test_call_tool_binary_response_surfaces_binaries():
    blob = b"\x89PNG\r\n\x1a\nfakepng"
    resp = _mock_response(content_type="image/png", content=blob, text="")
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[
        {"name": "img", "method": "GET", "path": "/img"},
    ])
    result, _ = _call(client, "img", {}, resp)
    assert result["success"] is True
    assert result["binaries"][0]["mime_type"] == "image/png"
    assert base64.b64decode(result["binaries"][0]["blob_b64"]) == blob


def test_server_url_property_aliases_base_url():
    client = CustomApiClient(base_url="https://api.example.com/v1/")
    assert client.server_url == "https://api.example.com/v1"


# ── Pinned (admin-fixed) parameters ────────────────────────────────────────

def _pinned_client(extra_params=None, method="GET"):
    params = [
        {"name": "sap-client", "in": "query", "value": "400"},
        {"name": "region", "in": "query", "type": "string"},
    ] + (extra_params or [])
    return CustomApiClient(base_url="https://api.example.com", endpoints=[
        {"name": "orders", "method": method, "path": "/orders", "parameters": params},
    ])


def test_pinned_param_hidden_from_input_schema():
    tools = _pinned_client().list_tools()
    props = tools[0]["input_schema"]["properties"]
    assert "sap-client" not in props
    assert "region" in props


def test_pinned_param_injected_on_every_call():
    client = _pinned_client()
    result, request = _call(client, "orders", {"region": "EMEA"}, _mock_response(json_data=[]))
    assert result["success"] is True
    assert request.call_args.kwargs["params"] == {"sap-client": "400", "region": "EMEA"}


def test_pinned_param_wins_over_model_argument():
    # A model-supplied value for a pinned name must neither override the pin
    # nor leak through the undeclared-args fallback.
    client = _pinned_client()
    _, request = _call(client, "orders", {"sap-client": "999"}, _mock_response(json_data=[]))
    assert request.call_args.kwargs["params"] == {"sap-client": "400"}


def test_pinned_header_and_path_params():
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[{
        "name": "t", "method": "GET", "path": "/tenants/{tenant}/orders",
        "parameters": [
            {"name": "tenant", "in": "path", "value": "acme"},
            {"name": "X-Env", "in": "header", "value": "prod"},
        ],
    }])
    _, request = _call(client, "t", {}, _mock_response(json_data=[]))
    args, kwargs = request.call_args
    assert args[1] == "https://api.example.com/tenants/acme/orders"
    assert kwargs["headers"]["X-Env"] == "prod"


# ── Explicit policy pass-through ───────────────────────────────────────────

def test_explicit_policy_emitted_for_confirm_flag():
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[
        {"name": "a", "method": "GET", "path": "/a", "confirm": True},
        {"name": "b", "method": "POST", "path": "/b", "confirm": False},
        {"name": "c", "method": "POST", "path": "/c"},
        {"name": "d", "method": "GET", "path": "/d"},
    ])
    tools = {t["name"]: t for t in client.list_tools()}
    assert tools["a"]["explicit_policy"] == "ask"
    assert tools["b"]["explicit_policy"] == "allow"
    assert "explicit_policy" not in tools["c"]
    assert tools["c"]["default_policy"] == "ask"      # write → ask (auto)
    assert "explicit_policy" not in tools["d"]
    assert "default_policy" not in tools["d"]         # read → allow (auto)


# ── CSRF token flow ────────────────────────────────────────────────────────

def _csrf_call(client, tool, args, get_responses, request_responses):
    """Run call_tool with mocked httpx, scripting the GET (token fetch) and
    request (actual call) responses. Returns (result, mock_instance)."""
    with patch("httpx.Client") as MockClient:
        inst = MockClient.return_value.__enter__.return_value
        inst.get.side_effect = get_responses
        inst.request.side_effect = request_responses
        result = client.call_tool(tool, args)
    return result, inst


def _csrf_client(**kwargs):
    return CustomApiClient(
        base_url="https://sap.example.com",
        csrf_token_flow=True,
        endpoints=[
            {"name": "create_order", "method": "POST", "path": "/orders"},
            {"name": "list_orders", "method": "GET", "path": "/orders"},
        ],
        **kwargs,
    )


def test_csrf_write_fetches_token_and_sends_it():
    fetch = _mock_response(json_data={})
    fetch.headers = {"x-csrf-token": "tok-123", "content-type": "application/json"}
    result, inst = _csrf_call(
        _csrf_client(), "create_order", {"x": 1},
        get_responses=[fetch],
        request_responses=[_mock_response(json_data={"ok": True})],
    )
    assert result["success"] is True
    # Token fetched from base URL with the Fetch marker…
    get_args, get_kwargs = inst.get.call_args
    assert get_args[0] == "https://sap.example.com/"
    assert get_kwargs["headers"]["X-CSRF-Token"] == "Fetch"
    # …and replayed on the write, inside the SAME client (shared cookies).
    sent = inst.request.call_args.kwargs["headers"]
    assert sent["X-CSRF-Token"] == "tok-123"


def test_csrf_get_does_not_fetch_token():
    result, inst = _csrf_call(
        _csrf_client(), "list_orders", {},
        get_responses=[],
        request_responses=[_mock_response(json_data=[])],
    )
    assert result["success"] is True
    inst.get.assert_not_called()


def test_csrf_disabled_does_not_fetch_token():
    client = CustomApiClient(base_url="https://api.example.com", endpoints=[
        {"name": "create", "method": "POST", "path": "/c"},
    ])
    result, inst = _csrf_call(
        client, "create", {},
        get_responses=[],
        request_responses=[_mock_response(json_data={})],
    )
    assert result["success"] is True
    inst.get.assert_not_called()


def test_csrf_stale_token_retries_once_with_fresh_token():
    fetch1 = _mock_response(json_data={})
    fetch1.headers = {"x-csrf-token": "stale", "content-type": "application/json"}
    fetch2 = _mock_response(json_data={})
    fetch2.headers = {"x-csrf-token": "fresh", "content-type": "application/json"}
    rejected = _mock_response(status=403, json_data={"error": "CSRF token validation failed"})
    rejected.headers = {"x-csrf-token": "Required", "content-type": "application/json"}
    ok = _mock_response(json_data={"ok": True})
    result, inst = _csrf_call(
        _csrf_client(), "create_order", {},
        get_responses=[fetch1, fetch2],
        request_responses=[rejected, ok],
    )
    assert result["success"] is True
    assert inst.get.call_count == 2
    assert inst.request.call_count == 2
    assert inst.request.call_args.kwargs["headers"]["X-CSRF-Token"] == "fresh"


def test_csrf_custom_fetch_path():
    fetch = _mock_response(json_data={})
    fetch.headers = {"x-csrf-token": "t", "content-type": "application/json"}
    client = CustomApiClient(
        base_url="https://sap.example.com",
        csrf_token_flow=True,
        csrf_fetch_path="/sap/opu/odata/sap/API_PO/$metadata",
        endpoints=[{"name": "create", "method": "POST", "path": "/c"}],
    )
    _, inst = _csrf_call(
        client, "create", {},
        get_responses=[fetch],
        request_responses=[_mock_response(json_data={})],
    )
    assert inst.get.call_args.args[0] == "https://sap.example.com/sap/opu/odata/sap/API_PO/$metadata"


def test_csrf_fetch_failure_still_sends_request():
    # Token endpoint unreachable → the write still goes out (server decides);
    # a hard fail here would turn a soft CSRF misconfig into total breakage.
    result, inst = _csrf_call(
        _csrf_client(), "create_order", {},
        get_responses=[RuntimeError("boom")],
        request_responses=[_mock_response(json_data={"ok": True})],
    )
    assert result["success"] is True
    assert "X-CSRF-Token" not in inst.request.call_args.kwargs["headers"]
