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
