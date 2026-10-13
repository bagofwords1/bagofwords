"""MCP "Dynamic params": an admin allow-list on the connection, values per question.

Contract (see app/services/mcp_context_injection.py, app/utils/request_metadata.py):
  * the connection config accepts ``metadata_injection.dynamic_params_enabled``
    + ``dynamic_params`` ({key: default}); a malformed allow-list is rejected
    with 400 on every save path;
  * ``POST /api/reports/{id}/completions`` accepts ``prompt.metadata`` (JSON
    object, <= 8KB, non-empty keys; 422 otherwise), stored with the completion,
    also when the prompt is queued;
  * every MCP ``tools/call`` of the run carries, inside ``custom_metadata`` and
    next to the fixed fields: each allowed key with the sent value, else its
    default (a null default is omitted); unlisted keys are dropped; a fixed
    field of the same name wins; toggle off -> nothing from the allow-list;
  * the values never reach the stored tool-call record the LLM replays, nor
    the public ``/c/{token}`` share page.

Real: routes, services, DB, the queue dispatcher, execute_mcp, McpClient and a
real streamable-HTTP MCP server (tests/mocks/echo_mcp_http_server.py) that
echoes what it received. Stubbed: only the LLM loop — a scripted AgentV2 whose
one "decision" is an execute_mcp call made through the REAL tool with the run's
own context.
"""
from __future__ import annotations

import json
import socket
import threading
import time
import uuid

import pytest
import uvicorn

import app.services.completion_service as completion_mod
from tests.mocks import echo_mcp_http_server as echo

ECHO_TOOL = "query_production_orders"
ALLOWED = {"chat_id": None, "ticket": None, "ctx": None, "env": "prod", "user_email": None}
META = {"chat_id": "c-77", "ticket": 4411, "ctx": {"beta": True, "tags": ["a", "b"]}}
FIXED = [{"name": "user_email", "source": "user.email", "mode": "locked"}]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def echo_url():
    port = _free_port()
    echo.mcp.settings.host = "127.0.0.1"
    echo.mcp.settings.port = port
    server = uvicorn.Server(
        uvicorn.Config(echo.mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "echo MCP server did not start"
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def capture(tmp_path, monkeypatch):
    path = tmp_path / "mcp_capture.json"
    monkeypatch.setattr(echo, "_CAPTURE_FILE", str(path))

    def read():
        return json.loads(path.read_text()) if path.exists() else None

    return read


class _ScriptedAgent:
    """Stand-in for AgentV2: one execute_mcp call through the REAL tool."""

    call: dict = {}
    results: list = []

    def __init__(self, **kw):
        self.kw = kw

    async def main_execution(self):
        from app.ai.tools.implementations.execute_mcp import ExecuteMCPTool
        from app.models.agent_execution import AgentExecution
        from app.models.tool_execution import ToolExecution
        from app.models.user import User

        db = self.kw["db"]
        head = self.kw["head_completion"]
        system = self.kw["system_completion"]
        report = self.kw["report"]
        org = self.kw["organization"]
        execution = AgentExecution(
            completion_id=str(system.id), organization_id=str(org.id), user_id=str(head.user_id),
            report_id=str(report.id), status="success",
        )
        db.add(execution)
        await db.flush()
        # Persist the tool call the way the agent does, then run it with the
        # same input object so a leak into the stored record would show.
        tool_input = json.loads(json.dumps(_ScriptedAgent.call))
        db.add(ToolExecution(agent_execution_id=str(execution.id), tool_name="execute_mcp",
                             arguments_json=tool_input, status="success"))
        await db.flush()
        user = await db.get(User, str(head.user_id))
        ctx = {"db": db, "user": user, "organization": org, "report": report,
               "head_completion": head, "system_completion": system}
        events = [e async for e in ExecuteMCPTool().run_stream(tool_input, ctx)]
        _ScriptedAgent.results.append(events[-1].payload)
        system.completion = {"content": "done"}
        system.status = "success"
        await db.commit()


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _config(echo_url, *, enabled=True, fields=FIXED, allowed=ALLOWED):
    return {
        "server_url": echo_url,
        "transport": "streamable_http",
        "metadata_injection": {
            "argument_key": "custom_metadata",
            "fields": list(fields),
            "dynamic_params_enabled": enabled,
            "dynamic_params": allowed,
        },
    }


@pytest.fixture
def env(test_client, create_user, login_user, whoami, create_data_source, create_report,
        refresh_connection_tools, enable_mcp, echo_url, monkeypatch):
    monkeypatch.setattr(completion_mod, "AgentV2", _ScriptedAgent)
    _ScriptedAgent.results = []

    email = f"dynparams_{uuid.uuid4().hex[:8]}@test.com"
    create_user(email=email, password="test123")
    token = login_user(email, "test123")
    org_id = whoami(token)["organizations"][0]["id"]
    headers = _h(token, org_id)

    # A default model so completions can run (no network: the agent is scripted).
    r = test_client.post("/api/llm/providers", headers=headers, json={
        "name": f"openai-{uuid.uuid4().hex[:6]}", "provider_type": "openai",
        "credentials": {"api_key": "sk-test-not-used"},
        "models": [{"model_id": "gpt-6-luna", "name": "GPT-6 Luna", "is_custom": False, "is_default": True}],
    })
    assert r.status_code == 200, r.text
    enable_mcp(token, org_id)

    ds = create_data_source(name="Echo MCP", type="mcp", config=_config(echo_url),
                            credentials={}, user_token=token, org_id=org_id)
    conn_id = next(c["id"] for c in ds["connections"])
    refresh_connection_tools(connection_id=conn_id, user_token=token, org_id=org_id)
    report_id = create_report(title="Orders", user_token=token, org_id=org_id, data_sources=[ds["id"]])["id"]
    _ScriptedAgent.call = {"connection_id": conn_id, "tool_name": ECHO_TOOL,
                           "arguments": {"prompt": "weekly orders", "company": "111"}}

    class E:
        pass

    e = E()
    e.client, e.headers, e.org_id, e.token = test_client, headers, org_id, token
    e.report_id, e.conn_id, e.email, e.echo_url = report_id, conn_id, email, echo_url

    def complete(metadata=None, **body):
        prompt = {"content": "weekly orders"}
        if metadata is not None:
            prompt["metadata"] = metadata
        return test_client.post(f"/api/reports/{report_id}/completions", headers=headers,
                                json={"prompt": prompt, **body})

    def run_turn(metadata=None):
        r = complete(metadata)
        assert r.status_code == 200, r.text
        assert _ScriptedAgent.results and _ScriptedAgent.results[-1]["output"]["success"], _ScriptedAgent.results

    def set_config(cfg):
        return test_client.put(f"/api/connections/{conn_id}", headers=headers, json={"config": cfg})

    e.run_turn, e.set_config, e.complete = run_turn, set_config, complete
    return e


@pytest.mark.e2e
def test_allowed_values_reach_the_mcp_server_next_to_fixed_fields(env, capture):
    env.run_turn(META)

    received = capture()["received_arguments"]
    assert received["custom_metadata"] == {**META, "env": "prod", "user_email": env.email}
    # The tool's own arguments are untouched.
    assert received["prompt"] == "weekly orders" and received["company"] == "111"


@pytest.mark.e2e
def test_each_question_carries_its_own_values(env, capture):
    env.run_turn({"chat_id": "c-1"})
    assert capture()["received_arguments"]["custom_metadata"]["chat_id"] == "c-1"
    env.run_turn({"chat_id": "c-2", "env": "staging"})
    cm = capture()["received_arguments"]["custom_metadata"]
    assert cm["chat_id"] == "c-2" and cm["env"] == "staging"


@pytest.mark.e2e
def test_question_without_metadata_sends_defaults_and_fixed_fields(env, capture):
    env.run_turn()
    # null defaults are omitted; "env" has a real default
    assert capture()["received_arguments"]["custom_metadata"] == {"env": "prod", "user_email": env.email}


@pytest.mark.e2e
def test_keys_not_in_the_allow_list_are_dropped(env, capture):
    env.run_turn({"chat_id": "c-77", "_client_userId": "spoofed", "anything": 1})
    cm = capture()["received_arguments"]["custom_metadata"]
    assert cm == {"chat_id": "c-77", "env": "prod", "user_email": env.email}


@pytest.mark.e2e
def test_fixed_field_wins_even_when_allowed(env, capture):
    env.run_turn({"user_email": "spoofed@example.com", "chat_id": "c-77"})
    cm = capture()["received_arguments"]["custom_metadata"]
    assert cm["user_email"] == env.email and cm["chat_id"] == "c-77"


@pytest.mark.e2e
def test_toggle_alone_without_fixed_fields(env, capture):
    assert env.set_config(_config(env.echo_url, fields=[])).status_code == 200
    env.run_turn({"chat_id": "c-77"})
    assert capture()["received_arguments"]["custom_metadata"] == {"chat_id": "c-77", "env": "prod"}


@pytest.mark.e2e
def test_toggle_off_sends_only_fixed_fields(env, capture):
    assert env.set_config(_config(env.echo_url, enabled=False)).status_code == 200
    env.run_turn(META)
    assert capture()["received_arguments"]["custom_metadata"] == {"user_email": env.email}


@pytest.mark.e2e
def test_allow_list_round_trips_in_the_saved_config(env, capture):
    got = env.client.get(f"/api/connections/{env.conn_id}", headers=env.headers)
    assert got.status_code == 200, got.text
    cfg = got.json()["config"]
    cfg = json.loads(cfg) if isinstance(cfg, str) else cfg
    assert cfg["metadata_injection"]["dynamic_params_enabled"] is True
    assert cfg["metadata_injection"]["dynamic_params"] == ALLOWED


@pytest.mark.e2e
def test_metadata_is_stored_with_the_completion(env, capture):
    env.run_turn(META)
    timeline = env.client.get(f"/api/reports/{env.report_id}/completions", headers=env.headers)
    assert timeline.status_code == 200, timeline.text
    prompts = [c["prompt"] for c in timeline.json()["completions"] if c["role"] == "user"]
    assert prompts and prompts[-1]["metadata"] == META


def _drive_dispatcher(report_id):
    """Run the queue dispatcher on a private loop until its dispatched run ends
    (same driver as test_completion_queue_steer.py)."""
    import asyncio

    async def _run():
        await completion_mod.CompletionService().start_next_queued_if_idle(report_id, None)
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.wait(pending, timeout=30)

    asyncio.run(_run())


@pytest.mark.e2e
def test_queued_question_keeps_its_metadata(env, capture, monkeypatch):
    # The route fires the dispatcher as a fire-and-forget task whose fate under
    # the sync TestClient depends on the DB backend. Hold it back so the row
    # stays queued, then drive the dispatcher: it starts the run from the row
    # as stored in the DB.
    real_dispatch = completion_mod.CompletionService.start_next_queued_if_idle

    async def _hold(self, *a, **kw):
        return None

    monkeypatch.setattr(completion_mod.CompletionService, "start_next_queued_if_idle", _hold)
    r = env.complete(META, queue=True)
    assert r.status_code == 200, r.text
    monkeypatch.setattr(completion_mod.CompletionService, "start_next_queued_if_idle", real_dispatch)
    assert capture() is None

    _drive_dispatcher(env.report_id)

    got = capture()
    assert got is not None, "the queued run never called the MCP server"
    assert got["received_arguments"]["custom_metadata"] == {**META, "env": "prod", "user_email": env.email}


@pytest.mark.e2e
def test_values_stay_out_of_the_stored_tool_call(env, capture):
    import asyncio
    from sqlalchemy import select
    from app.dependencies import async_session_maker
    from app.models.agent_execution import AgentExecution
    from app.models.tool_execution import ToolExecution

    env.run_turn(META)
    assert capture()["received_arguments"]["custom_metadata"]["ticket"] == 4411

    async def stored():
        async with async_session_maker() as s:
            rows = await s.execute(
                select(ToolExecution.arguments_json)
                .join(AgentExecution, AgentExecution.id == ToolExecution.agent_execution_id)
                .where(AgentExecution.report_id == env.report_id, ToolExecution.tool_name == "execute_mcp")
            )
            return [r[0] for r in rows.all()]

    records = asyncio.run(stored())
    assert records, "the tool call was not recorded"
    for args in records:
        assert "custom_metadata" not in (args or {}).get("arguments", {}), args


@pytest.mark.e2e
def test_public_share_page_hides_metadata(env, capture):
    env.run_turn(META)
    share = env.client.post(f"/api/reports/{env.report_id}/conversation-share", headers=env.headers)
    assert share.status_code == 200, share.text
    page = env.client.get(f"/api/c/{share.json()['token']}")
    assert page.status_code == 200, page.text
    prompts = [c["prompt"] for c in page.json()["completions"] if c["role"] == "user"]
    assert prompts and prompts[-1]["content"] == "weekly orders"
    assert all("metadata" not in p for p in prompts)


@pytest.mark.e2e
@pytest.mark.parametrize(
    "metadata, reason",
    [
        ("c-77", "not an object"),
        (["c-77"], "not an object"),
        ({"": 1}, "empty key"),
        ({"blob": "x" * (8 * 1024)}, "over 8KB"),
    ],
)
def test_invalid_metadata_is_rejected(env, capture, metadata, reason):
    r = env.complete(metadata)
    assert r.status_code == 422, (reason, r.text)
    assert capture() is None, "a rejected request must not run"


@pytest.mark.e2e
@pytest.mark.parametrize(
    "allowed, reason",
    [
        ("not-an-object", "JSON object"),
        ({"": 1}, "must not be empty"),
        ({"blob": "x" * (8 * 1024)}, "the limit is"),
    ],
)
def test_malformed_allow_list_is_rejected_on_update(env, capture, allowed, reason):
    r = env.set_config(_config(env.echo_url, allowed=allowed))
    assert r.status_code == 400, r.text
    assert reason in r.text


@pytest.mark.e2e
def test_malformed_allow_list_is_rejected_on_data_source_create(env, capture):
    r = env.client.post("/api/data_sources", headers=env.headers, json={
        "name": "Bad MCP", "type": "mcp", "credentials": {}, "is_public": True,
        "config": _config(env.echo_url, allowed=["not", "an", "object"]),
    })
    assert r.status_code == 400, r.text
