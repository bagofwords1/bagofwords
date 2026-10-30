"""An artifact's conversations with its viewer (``bow.conversations``),
``/api/artifacts/{id}/runtime/conversations``.

The contract:

* a signed-in member who may read the published artifact (or an editor, on
  the draft) holds conversations; the chat bubble's settings (Allow chat,
  "dashboard data only") neither switch them off nor narrow their agents;
* agents: Auto (every agent the viewer may use) unless the owner listed
  some or the report attached some; never wider than the viewer's access;
  no agent means refused, never "dashboard data only";
* the policy names the model each viewer's messages run on;
* each viewer holds any number of conversations and reaches only their own;
* a message streams NDJSON (started … completed, exactly one terminal
  event) and is stored, readable back through messages();
* an embed session holds conversations only on artifacts that allow its host;
* access is re-checked on every message (unshared: refused).

The agent loop is stubbed at AgentV2.main_execution (no LLM); routes,
services and the database are real.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import jwt
import pytest

from app.ai.agent_v2 import AgentV2
from tests.e2e.dashboards.dashboard_setup import as_, own, publish, share

pytestmark = pytest.mark.e2e

DASH = "conv_three_dash/dash_sales"
OTHER = "conv_three_dash/dash_stock"
ANSWER = "Rock leads with **80.19** USD."


@pytest.fixture
def stub_agent(monkeypatch, corpus, test_client):
    """The organization's default model (never called) and a stubbed agent loop."""
    r = test_client.post("/api/llm/providers", headers=as_(corpus, "admin"), json={
        "name": f"openai-{uuid.uuid4().hex[:6]}", "provider_type": "openai",
        "credentials": {"api_key": "sk-test-not-used"},
        "models": [{"model_id": "gpt-6-luna", "name": "GPT-6 Luna", "is_custom": False, "is_default": True}],
    })
    assert r.status_code == 200, r.text

    async def fake_main_execution(self):
        from app.models.completion_block import CompletionBlock

        self.system_completion.status = "success"
        self.system_completion.completion = {"content": ANSWER}
        self.db.add(self.system_completion)
        # What the planner leaves behind: the answer as the final block.
        self.db.add(CompletionBlock(completion_id=str(self.system_completion.id), source_type="final",
                                    block_index=0, title="Answer", status="completed", content=ANSWER))
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _chatty(corpus, corpus_rows, test_client, key=DASH, **settings):
    """A published app shared with the organization. The bubble stays off and
    data-only: conversations must not depend on it."""
    aid = own(corpus_rows, corpus, key)
    publish(aid)
    assert share(test_client, corpus, aid, "internal").status_code == 200
    corpus_rows.update("artifacts", aid, chat_enabled=False, chat_data_source_ids=[], **settings)
    return aid


def base(aid):
    return f"/api/artifacts/{aid}/runtime/conversations"


def events(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def test_who_may_hold_conversations(corpus, corpus_rows, test_client):
    aid = _chatty(corpus, corpus_rows, test_client)
    assert test_client.get(f"{base(aid)}/policy", headers=as_(corpus, "member")).json()["available"] is True
    assert test_client.post(base(aid), json={}, headers=as_(corpus, "member")).status_code == 201
    assert test_client.get(f"{base(aid)}/policy", headers=as_(corpus, "anonymous")).json()["reason"] == "auth_required"
    assert test_client.post(base(aid), json={}, headers=as_(corpus, "anonymous")).status_code == 401
    assert test_client.post(base(aid), json={}, headers=as_(corpus, "other_org")).status_code in (403, 404)
    # Not shared with the reader: as if it did not exist.
    hidden = own(corpus_rows, corpus, OTHER)
    publish(hidden)
    assert test_client.post(base(hidden), json={}, headers=as_(corpus, "member")).status_code == 404


def test_editors_try_conversations_on_the_unpublished_draft(corpus, corpus_rows, test_client):
    draft = own(corpus_rows, corpus, OTHER)  # never published
    assert test_client.post(base(draft), json={}, headers=as_(corpus, "owner")).status_code == 201
    assert test_client.post(base(draft), json={}, headers=as_(corpus, "member")).status_code == 404


def test_each_viewer_reaches_only_their_own_conversations(corpus, corpus_rows, test_client):
    aid = _chatty(corpus, corpus_rows, test_client)
    mine = [test_client.post(base(aid), json={"title": f"Q{i}"}, headers=as_(corpus, "member")).json() for i in range(3)]
    theirs = test_client.post(base(aid), json={"title": "theirs"}, headers=as_(corpus, "direct_share")).json()
    listed = test_client.get(base(aid), headers=as_(corpus, "member")).json()["items"]
    assert {c["id"] for c in listed} == {c["id"] for c in mine}
    assert [c["title"] for c in listed] == ["Q2", "Q1", "Q0"]  # newest first
    for path in (f"{base(aid)}/{theirs['id']}", f"{base(aid)}/{theirs['id']}/messages"):
        r = test_client.get(path, headers=as_(corpus, "member"))
        assert r.status_code == 404 and r.json()["error_code"] == "conversation.not_found"
    assert test_client.patch(f"{base(aid)}/{theirs['id']}", json={"title": "x"},
                             headers=as_(corpus, "member")).status_code == 404
    assert test_client.delete(f"{base(aid)}/{theirs['id']}", headers=as_(corpus, "member")).status_code == 404
    # A conversation belongs to its artifact too.
    other = _chatty(corpus, corpus_rows, test_client, OTHER)
    assert test_client.get(f"{base(other)}/{mine[0]['id']}", headers=as_(corpus, "member")).status_code == 404

    renamed = test_client.patch(f"{base(aid)}/{mine[0]['id']}", json={"title": "  Brazil   genres "},
                                headers=as_(corpus, "member"))
    assert renamed.status_code == 200 and renamed.json()["title"] == "Brazil genres"
    assert test_client.delete(f"{base(aid)}/{mine[1]['id']}", headers=as_(corpus, "member")).status_code == 204
    assert len(test_client.get(base(aid), headers=as_(corpus, "member")).json()["items"]) == 2


def _answer_id(corpus_rows, conv_id: str) -> str:
    from sqlalchemy import text

    with corpus_rows.engine.connect() as conn:
        return str(conn.execute(text("SELECT id FROM completions WHERE report_id = :r AND role = 'system'"),
                                {"r": conv_id}).scalar_one())


def _still_running(corpus_rows, conv_id: str, *, age_seconds: float = 0, **extra) -> str:
    """Marks the conversation's answer as a run still in progress, heartbeat
    `age_seconds` ago. A direct write: the stubbed agent finishes inside the
    request, so the API cannot hold a run open."""
    from datetime import datetime, timedelta

    cid = _answer_id(corpus_rows, conv_id)
    corpus_rows.update("completions", cid, status="in_progress",
                       updated_at=datetime.utcnow() - timedelta(seconds=age_seconds), **extra)
    return cid


def _flags(test_client, corpus, aid) -> dict:
    return {c["id"]: c["in_progress"] for c in test_client.get(base(aid), headers=as_(corpus, "member")).json()["items"]}


def test_the_list_marks_a_conversation_whose_answer_is_still_running(corpus, corpus_rows, test_client, stub_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    first, second = (test_client.post(base(aid), json={"title": t}, headers=as_(corpus, "member")).json() for t in ("A", "B"))
    for conv in (first, second):
        assert test_client.post(f"{base(aid)}/{conv['id']}/messages", json={"text": "Which genres sell best?"},
                                headers=as_(corpus, "member")).status_code == 200
    assert _flags(test_client, corpus, aid) == {first["id"]: False, second["id"]: False}

    _still_running(corpus_rows, first["id"])
    assert _flags(test_client, corpus, aid) == {first["id"]: True, second["id"]: False}

    # A run whose heartbeat stopped beyond the stale window is abandoned, not running.
    from app.services.completion_service import _COMPLETION_STALE_AFTER_SECONDS
    _still_running(corpus_rows, first["id"], age_seconds=_COMPLETION_STALE_AFTER_SECONDS + 60)
    assert _flags(test_client, corpus, aid)[first["id"]] is False

    # A sub-agent's run is not the conversation's answer.
    _still_running(corpus_rows, first["id"], trigger_source="sub_agent")
    assert _flags(test_client, corpus, aid)[first["id"]] is False


def test_archiving_a_conversation_stops_its_running_answer(corpus, corpus_rows, test_client, stub_agent):
    from sqlalchemy import text

    aid = _chatty(corpus, corpus_rows, test_client)
    conv, other = (test_client.post(base(aid), json={}, headers=as_(corpus, "member")).json() for _ in range(2))
    for c in (conv, other):
        assert test_client.post(f"{base(aid)}/{c['id']}/messages", json={"text": "Which genres sell best?"},
                                headers=as_(corpus, "member")).status_code == 200
    cid = _still_running(corpus_rows, conv["id"])
    other_cid = _still_running(corpus_rows, other["id"])

    # Another viewer can neither archive nor stop it.
    assert test_client.delete(f"{base(aid)}/{conv['id']}", headers=as_(corpus, "direct_share")).status_code == 404
    with corpus_rows.engine.connect() as conn:
        assert conn.execute(text("SELECT sigkill FROM completions WHERE id = :c"), {"c": cid}).scalar_one() is None

    assert test_client.delete(f"{base(aid)}/{conv['id']}", headers=as_(corpus, "member")).status_code == 204
    with corpus_rows.engine.connect() as conn:
        sigkill, status = conn.execute(text("SELECT sigkill, status FROM completions WHERE id = :c"), {"c": cid}).one()
    assert sigkill is not None and status == "stopped"
    # Only the archived conversation's answer is stopped.
    with corpus_rows.engine.connect() as conn:
        other_sigkill, other_status = conn.execute(text("SELECT sigkill, status FROM completions WHERE id = :c"),
                                                   {"c": other_cid}).one()
    assert other_sigkill is None and other_status == "in_progress"
    flags = _flags(test_client, corpus, aid)
    assert conv["id"] not in flags and flags[other["id"]] is True


def test_a_message_streams_once_and_is_stored(corpus, corpus_rows, test_client, stub_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    conv = test_client.post(base(aid), json={}, headers=as_(corpus, "member")).json()
    sent = test_client.post(f"{base(aid)}/{conv['id']}/messages", json={"text": "Which genres sell best?"},
                            headers=as_(corpus, "member"))
    assert sent.status_code == 200 and sent.headers["content-type"].startswith("application/x-ndjson"), sent.text
    stream = events(sent)
    assert stream[0]["type"] == "started" and stream[0]["message_id"]
    terminal = [e for e in stream if e["type"] in ("completed", "error")]
    assert terminal == [stream[-1]] and stream[-1] == {"type": "completed", "status": "success"}
    history = test_client.get(f"{base(aid)}/{conv['id']}/messages", headers=as_(corpus, "member")).json()["items"]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["text"] == "Which genres sell best?" and ANSWER in history[1]["text"]
    assert history[1]["id"] == stream[0]["message_id"]


def test_the_older_page_cursor_is_present_only_when_older_messages_exist(corpus, corpus_rows, test_client,
                                                                          stub_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    conv = test_client.post(base(aid), json={}, headers=as_(corpus, "member")).json()
    url = f"{base(aid)}/{conv['id']}/messages"

    def send(text):
        assert test_client.post(url, json={"text": text}, headers=as_(corpus, "member")).status_code == 200

    def read(**params):
        r = test_client.get(url, params=params, headers=as_(corpus, "member"))
        assert r.status_code == 200, r.text
        return r.json()

    send("first")
    # A short conversation fits in one page: no cursor, so no "Load earlier messages".
    assert read()["next_before"] is None
    assert read(limit=2)["next_before"] is None  # exactly the page size

    send("second")
    newest = read(limit=2)
    assert [m["text"] for m in newest["items"] if m["role"] == "user"] == ["second"]
    assert newest["next_before"]
    older = read(limit=2, before=newest["next_before"])
    assert [m["role"] for m in older["items"]] == ["user", "assistant"]
    assert older["items"][0]["text"] == "first"
    assert older["next_before"] is None


def test_a_stopped_answer_is_the_only_assistant_and_paging_skips_no_message(corpus, corpus_rows, test_client,
                                                                             stub_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    conv = test_client.post(base(aid), json={}, headers=as_(corpus, "member")).json()
    url = f"{base(aid)}/{conv['id']}/messages"

    def read(**params):
        r = test_client.get(url, params=params, headers=as_(corpus, "member"))
        assert r.status_code == 200, r.text
        return r.json()

    assert test_client.post(url, json={"text": "first"}, headers=as_(corpus, "member")).status_code == 200
    cid = _still_running(corpus_rows, conv["id"])
    assert test_client.post(f"{url}/{cid}/stop", headers=as_(corpus, "member")).status_code == 200
    # Stopping stores a session event row next to the answer; it is not a message.
    assert [(m["role"], m["status"]) for m in read()["items"]] == [("user", "success"), ("assistant", "stopped")]

    # Paging across the event row returns every message once, in order.
    assert test_client.post(url, json={"text": "second"}, headers=as_(corpus, "member")).status_code == 200
    seen, cursor = [], None
    for _ in range(5):
        page = read(limit=2, **({"before": cursor} if cursor else {}))
        seen = page["items"] + seen
        cursor = page["next_before"]
        if cursor is None:
            break
    assert cursor is None
    assert [(m["role"], m["status"]) for m in seen] == [
        ("user", "success"), ("assistant", "stopped"), ("user", "success"), ("assistant", "success")]
    assert [m["text"] for m in seen if m["role"] == "user"] == ["first", "second"]
    assert len({m["id"] for m in seen}) == 4


def test_access_is_rechecked_on_every_message(corpus, corpus_rows, test_client, stub_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    conv = test_client.post(base(aid), json={}, headers=as_(corpus, "member")).json()
    share(test_client, corpus, aid, "none")
    assert test_client.post(f"{base(aid)}/{conv['id']}/messages", json={"text": "hi"},
                            headers=as_(corpus, "member")).status_code == 404


def _release_agents(corpus_rows, aid) -> list[str]:
    from sqlalchemy import text

    with corpus_rows.engine.connect() as conn:
        release = conn.execute(text("select published_release_id from artifacts where id = :a"), {"a": aid}).scalar()
        return [str(r) for r in conn.execute(text(
            "select data_source_id from artifact_release_data_source_association where artifact_release_id = :r"),
            {"r": release}).scalars()]


def _as_auto(corpus_rows, aid):
    """The dashboard as built from an Auto report: its release names no agents."""
    from sqlalchemy import text

    with corpus_rows.engine.begin() as conn:
        release = conn.execute(text("select published_release_id from artifacts where id = :a"), {"a": aid}).scalar()
        conn.execute(text("delete from artifact_release_data_source_association where artifact_release_id = :r"),
                     {"r": release})


@pytest.fixture
def second_agent(corpus, sqlite_data_source):
    """Another agent the whole organization can use."""
    admin = corpus.cast.people["admin"]
    return sqlite_data_source(name="Second agent", user_token=admin.token, org_id=corpus.cast.org_id,
                              is_public=True)["id"]


def _policy(test_client, corpus, aid, person="member"):
    return test_client.get(f"{base(aid)}/policy", headers=as_(corpus, person)).json()


def test_an_app_built_on_auto_uses_every_agent_the_viewer_may_use(corpus, corpus_rows, test_client, second_agent):
    aid = _chatty(corpus, corpus_rows, test_client)  # bubble off and "data only": irrelevant to apps
    _as_auto(corpus_rows, aid)
    policy = _policy(test_client, corpus, aid)
    assert policy["available"] is True and policy["agents_mode"] == "auto"
    assert {corpus.cast.data_sources["chinook"], second_agent} <= {a["id"] for a in policy["agents"]}
    conv = test_client.post(base(aid), json={}, headers=as_(corpus, "member")).json()
    assert set(conv["agents"]) == {a["id"] for a in policy["agents"]}


def test_a_report_with_attached_agents_limits_its_app_to_them(corpus, corpus_rows, test_client, second_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    attached = _release_agents(corpus_rows, aid)
    assert attached, "the corpus report attaches its agent explicitly"
    policy = _policy(test_client, corpus, aid)
    assert policy["agents_mode"] == "limited" and {a["id"] for a in policy["agents"]} == set(attached)
    r = test_client.post(base(aid), json={"agents": [second_agent]}, headers=as_(corpus, "member"))
    assert r.status_code == 403 and r.json()["error_code"] == "conversation.agent_forbidden"


def test_the_owners_limit_wins_and_never_widens_a_viewers_access(corpus, corpus_rows, test_client, sqlite_data_source,
                                                                 second_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    _as_auto(corpus_rows, aid)
    corpus_rows.update("artifacts", aid, chat_data_source_ids=[second_agent])
    assert [a["id"] for a in _policy(test_client, corpus, aid)["agents"]] == [second_agent]
    # Limited to an agent this viewer may not use: no agent, refused, never "data only".
    admin = corpus.cast.people["admin"]
    private = sqlite_data_source(name="Private agent", user_token=admin.token, org_id=corpus.cast.org_id,
                                 is_public=False)["id"]
    corpus_rows.update("artifacts", aid, chat_data_source_ids=[private])
    policy = _policy(test_client, corpus, aid)
    assert policy["available"] is False and policy["reason"] == "no_agents"
    r = test_client.post(base(aid), json={}, headers=as_(corpus, "member"))
    assert r.status_code == 403 and r.json()["error_code"] == "conversation.no_agents"


def test_a_narrowed_conversation_keeps_its_agents(corpus, corpus_rows, test_client, second_agent, stub_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    _as_auto(corpus_rows, aid)
    chinook = corpus.cast.data_sources["chinook"]
    conv = test_client.post(base(aid), json={"agents": [chinook]}, headers=as_(corpus, "member")).json()
    assert conv["agents"] == [chinook]
    events(test_client.post(f"{base(aid)}/{conv['id']}/messages", json={"text": "hi"}, headers=as_(corpus, "member")))
    again = test_client.get(f"{base(aid)}/{conv['id']}", headers=as_(corpus, "member")).json()
    assert again["agents"] == [chinook]


def test_the_policy_names_the_model_a_viewer_runs_on(corpus, corpus_rows, test_client, stub_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    unpinned = _policy(test_client, corpus, aid)["model"]
    assert unpinned and unpinned["source"] == "default"
    models = test_client.get("/api/llm/models", headers=as_(corpus, "admin")).json()
    other = next(m for m in models if m["id"] != unpinned["id"])
    corpus_rows.update("artifacts", aid, chat_model_id=other["id"])
    pinned = _policy(test_client, corpus, aid)["model"]
    assert pinned == {"id": other["id"], "name": pinned["name"], "source": "pinned"}


def test_an_embed_session_holds_conversations_only_where_its_host_is_allowed(corpus, corpus_rows, test_client, stub_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    admin = as_(corpus, "admin")
    membership = corpus.cast.people["member"].membership_id
    test_client.put(f"/api/organizations/{corpus.cast.org_id}/members/{membership}",
                    json={"custom_attributes": {"employee_id": "E-55"}}, headers=admin)
    host = test_client.post("/api/host_applications", json={
        "name": "Back office", "allowed_origins": ["https://erp.example.com"], "identity_mode": "assertion",
        "identity_source": "member_attribute:employee_id", "issuer": "erp"}, headers=admin).json()
    now = int(time.time())
    assertion = jwt.encode({"iss": "erp", "aud": host["public_id"], "sub": "E-55", "iat": now, "exp": now + 60,
                            "jti": uuid.uuid4().hex}, host["private_key"], algorithm="RS256")
    token = test_client.post(f"/api/embed/{host['public_id']}/session", json={"assertion": assertion}).json()["token"]
    embed = {"Authorization": f"Bearer {token}"}
    refused = test_client.post(base(aid), json={}, headers=embed)
    assert refused.status_code == 403 and refused.json()["error_code"] == "embed.not_allowed"
    assert test_client.put(f"/api/artifacts/{aid}/embedding", json={"host_application_ids": [host["id"]]},
                           headers=as_(corpus, "owner")).status_code == 200
    conv = test_client.post(base(aid), json={}, headers=embed)
    assert conv.status_code == 201
    sent = test_client.post(f"{base(aid)}/{conv.json()['id']}/messages", json={"text": "hi"}, headers=embed)
    assert events(sent)[-1]["type"] == "completed"
    # The same person in BOW sees the conversation they had in the back office.
    listed = test_client.get(base(aid), headers=as_(corpus, "member")).json()["items"]
    assert conv.json()["id"] in {c["id"] for c in listed}


# ── data results (create_data) in the stream and in history ─────────────

GENRES = [{"genre": g, "sales": s} for g, s in [("Rock", 80.19), ("Latin", 41.58), ("Metal", 31.68), ("Alternative", 23.76),
                                                ("Jazz", 15.84), ("Blues", 9.9), ("Classical", 8.91), ("Pop", 6.93),
                                                ("Reggae", 5.94), ("Soundtrack", 4.95), ("TV Shows", 3.98), ("Drama", 1.99)]]


def _grid(rows, dtypes=None):
    cols = list(rows[0]) if rows else []
    return {"rows": rows, "columns": [{"headerName": c, "field": c} for c in cols],
            "info": {"total_rows": len(rows), "total_columns": len(cols),
                     "column_info": {c: {"dtype": d} for c, d in (dtypes or {}).items()}}}


SALES = _grid(GENRES, {"genre": "object", "sales": "float64"})
BAR = {"version": "v2", "view": {"type": "bar_chart", "x": "genre", "y": "sales"}}


@pytest.fixture
def data_agent(stub_agent, monkeypatch):
    """The agent loop stubbed at AgentV2.main_execution, leaving what a
    create_data call leaves: the thread's query, step and visualization, the
    run, the tool execution and its block. The rows are written with the ORM
    because only the agent loop writes them (no API can). The SSE frames the
    stub emits, and their order, are the scenario's (``order``):

    * "sqlite": tool.started, the final block.upsert, tool.finished (the single-writer path);
    * "postgres": tool.started, tool.finished, completion.finished, then the
      rows are committed and the final block.upsert follows (the background persist);
    * "none": no tool frames at all.
    """
    plan = {"order": "sqlite", "results": [dict(grid=SALES, view=BAR, title="Sales by genre")]}

    async def fake_main_execution(self):
        from app.models.agent_execution import AgentExecution
        from app.models.completion_block import CompletionBlock
        from app.models.query import Query
        from app.models.step import Step
        from app.models.tool_execution import ToolExecution
        from app.models.visualization import Visualization
        from app.schemas.sse_schema import SSEEvent
        from app.serializers.completion_v2 import serialize_block_v2

        thread, org, uid = str(self.report.id), str(self.organization.id), str(self.report.user_id)
        message = str(self.system_completion.id)
        order = plan["order"]

        async def emit(event, data):
            await self._emit_sse_event(SSEEvent(event=event, completion_id=message, data=data))

        execution = AgentExecution(completion_id=message, organization_id=org, user_id=uid, report_id=thread,
                                   status="success")
        self.db.add(execution)
        await self.db.flush()
        tes = [str(uuid.uuid4()) for _ in plan["results"]]
        if order != "none":
            for te, spec in zip(tes, plan["results"]):
                await emit("tool.started", {"tool_name": "create_data", "tool_execution_id": te, "block_id": None,
                                            "arguments": {"title": spec.get("args_title", spec["title"])}})
        if order == "postgres":
            for te in tes:
                await emit("tool.finished", {"tool_name": "create_data", "tool_execution_id": te, "status": "success"})
            await emit("completion.finished", {"status": "success"})
        for i, (te, spec) in enumerate(zip(tes, plan["results"])):
            step_id = spec.get("step_id")
            viz_ids = []
            if step_id is None:
                query = Query(title=spec["title"], report_id=thread, organization_id=org, user_id=uid)
                self.db.add(query)
                await self.db.flush()
                step = Step(title=spec.get("step_title", spec["title"]), slug=f"step-{uuid.uuid4().hex}",
                            status="success", data=spec["grid"], query_id=str(query.id))
                viz = Visualization(title=spec["title"], status="success", report_id=thread, query_id=str(query.id),
                                    view=spec["view"])
                self.db.add_all([step, viz])
                await self.db.flush()
                step_id, viz_ids = str(step.id), [str(viz.id)]
            self.db.add(ToolExecution(id=te, agent_execution_id=str(execution.id), tool_name="create_data",
                                      arguments_json={"title": spec.get("args_title", spec["title"])},
                                      status="success", success=True, created_step_id=step_id,
                                      artifact_refs_json={"visualizations": viz_ids}))
            await self.db.flush()
            block = CompletionBlock(completion_id=message, agent_execution_id=str(execution.id), source_type="tool",
                                    tool_execution_id=te, block_index=i, title=spec["title"], status="completed")
            self.db.add(block)
            await self.db.commit()
            if order != "none":
                await emit("block.upsert", {"block": (await serialize_block_v2(self.db, block)).model_dump()})
            if order == "sqlite":
                await emit("tool.finished", {"tool_name": "create_data", "tool_execution_id": te, "status": "success",
                                             "created_step_id": step_id})
        self.system_completion.status = "success"
        self.system_completion.completion = {"content": ANSWER}
        self.db.add(self.system_completion)
        self.db.add(CompletionBlock(completion_id=message, source_type="final", block_index=len(tes),
                                    title="Answer", status="completed", content=ANSWER))
        await self.db.commit()

    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)
    return plan


def _send(test_client, corpus, aid, person="member", text="Which genres sell best?"):
    conv = test_client.post(base(aid), json={}, headers=as_(corpus, person)).json()
    sent = test_client.post(f"{base(aid)}/{conv['id']}/messages", json={"text": text}, headers=as_(corpus, person))
    assert sent.status_code == 200, sent.text
    history = test_client.get(f"{base(aid)}/{conv['id']}/messages", headers=as_(corpus, person))
    assert history.status_code == 200, history.text
    return conv, sent, events(sent), history.json()["items"]


def _assert_last_completed(stream):
    terminal = [i for i, e in enumerate(stream) if e["type"] in ("completed", "error")]
    assert terminal == [len(stream) - 1] and stream[-1] == {"type": "completed", "status": "success"}


@pytest.mark.parametrize("order", ["sqlite", "postgres"])
def test_a_data_answer_streams_its_result_before_completed_and_keeps_it_in_history(
        corpus, corpus_rows, test_client, data_agent, order):
    data_agent["order"] = order
    aid = _chatty(corpus, corpus_rows, test_client)
    _, _, stream, history = _send(test_client, corpus, aid)
    _assert_last_completed(stream)
    results = [e for e in stream if e["type"] == "result"]
    assert len(results) == 1
    r = results[0]
    assert len(r["rows"]) == 12 and r["rows"] == GENRES and r["row_count"] == 12 and r["truncated"] is False
    assert r["chart"] == {"type": "bar", "x": "genre", "y": ["sales"], "series": None}
    assert {c["field"]: c["type"] for c in r["columns"]} == {"genre": "string", "sales": "number"}
    tool_lines = [i for i, e in enumerate(stream) if e["type"] == "tool" and e["id"] == r["block"]]
    assert tool_lines and tool_lines[0] < stream.index(r)  # after its block's tool line
    # History: the same result on the same message.
    assistant = [m for m in history if m["role"] == "assistant"]
    assert len(assistant) == 1 and assistant[0]["id"] == stream[0]["message_id"]
    assert assistant[0]["results"] == [{k: v for k, v in r.items() if k != "type"}]
    assert [m for m in history if m["role"] == "user" and "results" in m] == []


def test_a_result_with_no_tool_frames_is_still_sent_before_completed(corpus, corpus_rows, test_client, data_agent):
    data_agent["order"] = "none"
    aid = _chatty(corpus, corpus_rows, test_client)
    _, _, stream, history = _send(test_client, corpus, aid)
    _assert_last_completed(stream)
    results = [e for e in stream if e["type"] == "result"]
    assert len(results) == 1 and results[0]["rows"] == GENRES
    assert history[-1]["results"][0]["id"] == results[0]["id"]


def test_a_text_answer_has_no_results(corpus, corpus_rows, test_client, stub_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    _, _, stream, history = _send(test_client, corpus, aid)
    _assert_last_completed(stream)
    assert [e for e in stream if e["type"] == "result"] == []
    assert [m["role"] for m in history] == ["user", "assistant"] and history[1]["results"] == []
    assert ANSWER in history[1]["text"] and history[1]["tools"] == []


def _release_step(aid) -> str:
    """A step of the published dashboard (its copies carry the creator's rows)."""
    from sqlalchemy import select

    from app.dependencies import async_session_maker
    from app.models.artifact import Artifact
    from app.models.query import Query
    from app.models.step import Step

    async def go():
        async with async_session_maker() as db:
            artifact = await db.get(Artifact, aid)
            return str((await db.execute(select(Step.id).join(Query, Query.id == Step.query_id).where(
                Query.artifact_release_id == str(artifact.published_release_id)).limit(1))).scalar_one())

    return asyncio.run(go())


def _foreign_thread_step(test_client, corpus, aid) -> str:
    """A step in another viewer's conversation on the same artifact. The thread
    is created through the API; its query and step are what an agent run in it
    would have left (ORM: only the agent loop writes them)."""
    from app.dependencies import async_session_maker
    from app.models.query import Query
    from app.models.step import Step

    theirs = test_client.post(base(aid), json={}, headers=as_(corpus, "direct_share")).json()

    async def go():
        async with async_session_maker() as db:
            query = Query(title="Their sales", report_id=theirs["id"], organization_id=corpus.cast.org_id,
                          user_id=corpus.cast.id("direct_share"))
            db.add(query)
            await db.flush()
            step = Step(title="Their sales", slug=f"step-{uuid.uuid4().hex}", status="success", data=SALES,
                        query_id=str(query.id))
            db.add(step)
            await db.commit()
            return str(step.id)

    return asyncio.run(go())


@pytest.mark.parametrize("person", ["member", "admin"])
def test_a_step_outside_the_viewers_thread_is_never_a_result(corpus, corpus_rows, test_client, data_agent, person):
    aid = _chatty(corpus, corpus_rows, test_client)
    outside = [_release_step(aid), _foreign_thread_step(test_client, corpus, aid)]
    # The tool's created_step_id falls back to a step it read (read_query and co.).
    data_agent["results"] = [dict(step_id=s, title=f"Read {i}") for i, s in enumerate(outside)]
    _, _, stream, history = _send(test_client, corpus, aid, person=person)
    assert [e for e in stream if e["type"] == "result"] == []
    assert [e["type"] for e in stream if e["type"] == "tool"] == ["tool", "tool"]  # the tools still show
    assert history[-1]["results"] == []
    # Two successful create_data whose tables are never shown: both say so.
    assert stream[-1] == {"type": "completed", "status": "success", "results_incomplete": True}
    assert history[-1]["results_incomplete"] is True


def test_the_surface_is_per_message_on_a_shared_thread(corpus, corpus_rows, test_client, stub_agent):
    from sqlalchemy import select

    from app.models.completion import Completion

    aid = _chatty(corpus, corpus_rows, test_client)
    corpus_rows.update("artifacts", aid, chat_enabled=True)  # the bubble on
    conv, sent, _, _ = _send(test_client, corpus, aid, text="asked in the app")
    bubble = test_client.post(f"/api/d/{aid}/chat/completions", json={"prompt": {"content": "asked in the bubble"}},
                              headers=as_(corpus, "member"))
    assert bubble.status_code == 200, bubble.text
    table = Completion.__table__  # its typed prompt column decrypts the stored value
    with corpus_rows.engine.connect() as c:
        prompts = [json.loads(p) if isinstance(p, str) else p for (p,) in c.execute(
            select(table.c.prompt).where(table.c.report_id == conv["id"], table.c.role == "user"))]
    by_text = {p["content"]: p.get("platform_context") or {} for p in prompts}
    assert set(by_text) == {"asked in the app", "asked in the bubble"}  # the bubble continued the same thread
    assert by_text["asked in the app"].get("surface") == "app"
    assert "surface" not in by_text["asked in the bubble"]


def test_results_and_tool_titles_are_redacted_for_display(corpus, corpus_rows, test_client, data_agent, monkeypatch,
                                                          _e2e_force_enterprise_license):
    import app.ai.llm.pii.loader as loader
    from app.ee import license as ee_license

    # The corpus's RBAC license fake lists no pii_protection; the suite's
    # session license (tests/e2e/conftest.py) does.
    monkeypatch.setattr(ee_license, "get_license_info", lambda force_refresh=False: _e2e_force_enterprise_license)
    a, b = f"ann.{uuid.uuid4().hex[:8]}@example.com", f"bo.{uuid.uuid4().hex[:8]}@example.org"
    put = test_client.put("/api/organization/settings", headers=as_(corpus, "admin"),
                          json={"config": {"pii_protection": {"enabled": True, "mode": "replace"}}})
    assert put.status_code == 200, put.text
    loader.invalidate()
    rows = [{"customer": f"Contact {a}", "sales": 12.5}, {"customer": "Walk-in", "sales": 3}]
    data_agent["results"] = [dict(grid=_grid(rows, {"customer": "object", "sales": "float64"}), view=None,
                                  title="Top customers", step_title=f"Top customers of {a}", args_title=f"Ask {b}")]
    try:
        aid = _chatty(corpus, corpus_rows, test_client)
        _, sent, stream, history = _send(test_client, corpus, aid)
        hist = json.dumps(history)
        for raw in (a, b):
            assert raw not in sent.text and raw not in hist
        mask = "[REDACTED_EMAIL]"
        r = next(e for e in stream if e["type"] == "result")
        assert r["rows"][0]["customer"] == f"Contact {mask}" and mask in r["title"]
        assert [c["field"] for c in r["columns"]] == ["customer", "sales"]
        assert [x["sales"] for x in r["rows"]] == [12.5, 3]
        assert any(e["type"] == "tool" and mask in (e["title"] or "") for e in stream)
        assistant = history[-1]
        assert assistant["results"] == [{k: v for k, v in r.items() if k != "type"}]
        assert assistant["tools"] and all(mask in t["title"] for t in assistant["tools"])
    finally:
        loader.invalidate()


def test_results_and_tool_titles_are_withheld_when_the_pii_settings_cannot_be_read(
        corpus, corpus_rows, test_client, data_agent, monkeypatch, _e2e_force_enterprise_license):
    import app.ai.llm.pii.loader as loader
    from app.ee import license as ee_license

    monkeypatch.setattr(ee_license, "get_license_info", lambda force_refresh=False: _e2e_force_enterprise_license)
    put = test_client.put("/api/organization/settings", headers=as_(corpus, "admin"),
                          json={"config": {"pii_protection": {"enabled": True, "mode": "replace"}}})
    assert put.status_code == 200, put.text

    def unreadable(_config):  # the loader's ruleset build: the org's PII rules cannot be loaded
        raise RuntimeError("ruleset failed to compile")

    monkeypatch.setattr(loader, "build_redactor", unreadable)
    loader.invalidate()
    a, b = f"ann.{uuid.uuid4().hex[:8]}@example.com", f"bo.{uuid.uuid4().hex[:8]}@example.org"
    rows = [{"customer": f"Contact {a}", "sales": 12.5}]
    data_agent["results"] = [dict(grid=_grid(rows, {"customer": "object", "sales": "float64"}), view=None,
                                  title=f"Top customers of {a}", args_title=f"Ask {b}")]
    try:
        aid = _chatty(corpus, corpus_rows, test_client)
        _, sent, stream, history = _send(test_client, corpus, aid)
    finally:
        loader.invalidate()
    for raw in (a, b):
        assert raw not in sent.text and raw not in json.dumps(history)
    assert [e for e in stream if e["type"] == "result"] == []
    assert [e["title"] for e in stream if e["type"] == "tool"] == [None]
    assert stream[-1] == {"type": "completed", "status": "success", "results_incomplete": True}
    assistant = history[-1]
    assert ANSWER in assistant["text"]  # the text answer still flows
    assert assistant["results"] == [] and assistant["results_incomplete"] is True
    assert [t["title"] for t in assistant["tools"]] == [None]


def test_large_results_stay_within_the_message_budget_in_stream_and_history(
        corpus, corpus_rows, test_client, data_agent):
    from app.services.artifact_conversation_results import MAX_MESSAGE_RESULT_BYTES, result_line

    big = _grid([{"t": f"{i:05d}" + "q" * 1495, "u": f"{i:05d}" + "r" * 1495} for i in range(200)])
    data_agent["order"] = "none"
    data_agent["results"] = [dict(grid=big, view=None, title=f"Part {i}") for i in range(3)]
    aid = _chatty(corpus, corpus_rows, test_client)
    _, _, stream, history = _send(test_client, corpus, aid)
    _assert_last_completed(stream)
    streamed = [{k: v for k, v in e.items() if k != "type"} for e in stream if e["type"] == "result"]
    assert 2 <= len(streamed) <= 3
    assert sum(len(result_line(r)) for r in streamed) <= MAX_MESSAGE_RESULT_BYTES
    assert [r["title"] for r in streamed][:2] == ["Part 0", "Part 1"]
    assert history[-1]["results"] == streamed  # the same, in block order
    # A third that kept rows is complete; one dropped (or cut to no rows) is said to be missing.
    lost = len(streamed) == 2 or not streamed[2]["rows"]
    assert stream[-1].get("results_incomplete", False) is lost
    assert history[-1].get("results_incomplete", False) is lost
