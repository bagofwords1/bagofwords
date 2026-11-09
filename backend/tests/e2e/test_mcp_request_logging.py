"""What the MCP server writes to its logs.

Contract: a request to ``/api/mcp`` may be logged by method, tool name,
JSON-RPC id and error type. The content the client sent (the body, tool
arguments, the Accept header value) and any part of a bearer token never
reaches the log, also when the request fails to validate or the tool fails.

Real: routes, auth, MCP tools, services, DB. Nothing is mocked; the logs are
read through caplog attached to the ``app`` logger tree.
"""
import json
import logging
import secrets
import uuid

import pytest


def _capture_app_logs(monkeypatch, caplog):
    """Collect every ``app.*`` record, DEBUG and up.

    The app's logging setup (alembic's fileConfig) disables loggers that exist
    when it runs, so enable them here rather than depend on test order.
    """
    for name, lg in list(logging.root.manager.loggerDict.items()):
        if (name == "app" or name.startswith("app.")) and isinstance(lg, logging.Logger):
            monkeypatch.setattr(lg, "disabled", False)
    app_logger = logging.getLogger("app")
    monkeypatch.setattr(app_logger, "handlers", [caplog.handler])
    monkeypatch.setattr(app_logger, "propagate", False)
    caplog.set_level(logging.DEBUG, logger="app")
    logging.getLogger("app.routes.mcp").info("capture is active")
    assert "capture is active" in caplog.text
    caplog.clear()


def _leaks(caplog, *needles):
    """Records whose message or traceback contains any needle."""
    hits = []
    for record in caplog.records:
        text = record.getMessage()
        if record.exc_info:
            text += "\n" + logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            text += "\n" + record.exc_text
        if any(n in text for n in needles):
            hits.append(f"{record.name}:{record.levelname}: {text[:200]!r}")
    return hits


def _mcp_records(caplog):
    return [r for r in caplog.records if r.name == "app.routes.mcp"]


@pytest.fixture
def mcp_owner(create_user, login_user, whoami, create_api_key, enable_mcp, create_report):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    enable_mcp(user_token=token, org_id=org_id)
    api_key = create_api_key(user_token=token, org_id=org_id)["key"]
    report = create_report(title=f"mcp-logs-{uuid.uuid4().hex[:6]}", user_token=token,
                           org_id=org_id, data_sources=[])
    return {"token": token, "org_id": org_id, "api_key": api_key, "report_id": report["id"]}


def _marker(label):
    return f"{label}-{secrets.token_hex(8)}"


@pytest.mark.e2e
@pytest.mark.parametrize("arguments_kind", ["valid", "invalid"])
@pytest.mark.parametrize("transport", ["json", "sse"])
def test_tools_call_logs_no_arguments_or_accept_header(
    transport, arguments_kind, monkeypatch, caplog, test_client, mcp_owner,
):
    """A tool call, whether its arguments validate or not, over plain JSON or
    SSE, logs neither the arguments nor the Accept header value."""
    secret_arg = _marker("arg-secret")
    accept_marker = _marker("accept-marker")
    patterns = [secret_arg] if arguments_kind == "valid" else secret_arg  # a string where a list belongs
    accept = f"application/json, application/x-{accept_marker}"
    if transport == "sse":
        accept += ", text/event-stream"
    _capture_app_logs(monkeypatch, caplog)

    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {
            "name": "get_context",
            "arguments": {"report_id": mcp_owner["report_id"], "patterns": patterns},
        }},
        headers={"X-API-Key": mcp_owner["api_key"], "Accept": accept},
    )

    assert response.status_code == 200, response.text
    if transport == "sse":
        payload = json.loads(response.text.split("data: ", 1)[1].split("\n", 1)[0])
    else:
        payload = response.json()
    assert payload["result"]["isError"] is (arguments_kind == "invalid"), payload

    leaks = _leaks(caplog, secret_arg, accept_marker)
    assert not leaks, f"MCP logs carry request content: {leaks}"
    # The request is still traceable in the log by tool name.
    assert any("get_context" in r.getMessage() for r in _mcp_records(caplog)), \
        [r.getMessage() for r in caplog.records]


@pytest.mark.e2e
def test_invalid_json_rpc_envelope_logs_no_submitted_values(
    monkeypatch, caplog, test_client, mcp_owner,
):
    """A request whose envelope fails validation is logged without the values
    it carried (a pydantic error quotes them as input_value)."""
    secret = _marker("envelope-secret")
    _capture_app_logs(monkeypatch, caplog)

    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": secret},
        headers={"X-API-Key": mcp_owner["api_key"]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["error"]["code"] == -32600
    leaks = _leaks(caplog, secret)
    assert not leaks, f"MCP logs carry request content: {leaks}"
    assert any(r.levelno >= logging.WARNING for r in _mcp_records(caplog)), \
        "an invalid request should still be logged"


@pytest.mark.e2e
def test_failed_oauth_token_is_logged_without_any_token_characters(
    monkeypatch, caplog, test_client, mcp_owner,
):
    """A bearer token that fails validation is logged as a failure only: no
    character past the public ``bow_oauth_`` prefix reaches the log."""
    secret_part = secrets.token_urlsafe(24)
    token = f"bow_oauth_{secret_part}"
    _capture_app_logs(monkeypatch, caplog)

    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 9, "method": "tools/list"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401
    # Any 4 consecutive characters of the secret part would be a leak.
    needles = {secret_part[i:i + 4] for i in range(len(secret_part) - 3)}
    leaks = _leaks(caplog, *needles)
    assert not leaks, f"MCP logs carry token characters: {leaks}"
    assert any(r.levelno >= logging.WARNING and "oauth" in r.getMessage().lower()
               for r in _mcp_records(caplog)), "the failed OAuth validation should still be logged"


@pytest.mark.e2e
def test_failed_saved_query_run_is_logged_without_the_values_in_its_error(
    monkeypatch, caplog, test_client, mcp_owner, create_data_source, create_global_entity,
    dynamic_sqlite_db,
):
    """describe_entity runs a saved query for the caller. When the run fails
    with an error that quotes a value (as driver and conversion errors quote
    cells), the log names the failure, not the value."""
    marker = _marker("cellvalue")
    ds = create_data_source(
        name=f"Warehouse {uuid.uuid4().hex[:6]}", type="sqlite",
        config={"database": dynamic_sqlite_db}, credentials={},
        user_token=mcp_owner["token"], org_id=mcp_owner["org_id"],
    )
    suffix = uuid.uuid4().hex[:8]
    entity = create_global_entity(
        title=f"Broken metric {suffix}", slug=f"broken-metric-{suffix}", status="published",
        # int() of a non-number fails with the value in the message, the shape
        # of "could not convert string to float: <cell>".
        type="metric", code=f"value = int('{marker}')",
        data_source_ids=[ds["id"]], user_token=mcp_owner["token"], org_id=mcp_owner["org_id"],
    )
    _capture_app_logs(monkeypatch, caplog)

    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {
            "name": "describe_entity", "arguments": {"name_or_id": entity["id"], "run": True},
        }},
        headers={"X-API-Key": mcp_owner["api_key"]},
    )

    assert response.status_code == 200, response.text
    result = json.loads(response.json()["result"]["content"][0]["text"])
    assert result.get("error_message"), f"the saved query was expected to fail: {result}"
    leaks = _leaks(caplog, marker)
    assert not leaks, f"MCP logs carry values from the saved query's run: {leaks}"
    assert any("describe_entity" in r.getMessage() and r.levelno >= logging.WARNING
               for r in caplog.records), "the failed run should still be logged"


@pytest.mark.e2e
def test_failed_create_instruction_logs_neither_title_nor_text(
    monkeypatch, caplog, test_client, mcp_owner, db_backend,
):
    """create_instruction with a title longer than its column (String(255))
    fails at the INSERT on Postgres. A DB error's text carries the statement's
    bound parameters, so the log names the failure, not the title or text."""
    if db_backend == "sqlite":
        pytest.skip("SQLite does not enforce VARCHAR lengths, so the INSERT succeeds")
    title_marker = _marker("title-secret")
    text_marker = _marker("text-secret")
    title = (title_marker + " ") * 20
    assert len(title) > 255
    _capture_app_logs(monkeypatch, caplog)

    response = test_client.post(
        "/api/mcp",
        json={"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {
            "name": "create_instruction",
            "arguments": {"text": f"Always {text_marker}.", "title": title},
        }},
        headers={"X-API-Key": mcp_owner["api_key"]},
    )

    assert response.status_code == 200, response.text
    envelope = response.json()["result"]
    assert envelope["isError"] is False, \
        f"the tool raised instead of reporting the failure: {envelope['content'][0]['text'][:120]}"
    result = json.loads(envelope["content"][0]["text"])
    assert result.get("success") is False, f"the INSERT was expected to fail: {result}"
    leaks = _leaks(caplog, title_marker, text_marker)
    assert not leaks, f"MCP logs carry the instruction's title or text: {leaks}"
    assert any("create_instruction" in r.getMessage() and r.levelno >= logging.ERROR
               for r in caplog.records), "the failed create should still be logged"
