"""Which outside app a conversation came through (app/core/request_origin.py).

The contract:

* a conversation held in an app embedded in a host application lists in its
  owner's reports, named after that host application, and only theirs;
* the same app used inside BOW adds nothing to the reports list;
* Monitoring names the app on each run that came through it;
* a conversation started with an OAuth app's token is named after that app;
* names are the apps' current names.

The agent loop is stubbed at AgentV2.main_execution; it records the run the
way the real loop does, so Monitoring has a row.
"""
from __future__ import annotations

import time
import uuid

import jwt
import pytest

from app.ai.agent_v2 import AgentV2
from tests.e2e.dashboards.dashboard_setup import as_
from tests.e2e.dashboards.test_artifact_conversations import _chatty, base

pytestmark = pytest.mark.e2e


@pytest.fixture
def stub_agent(monkeypatch, corpus, test_client):
    r = test_client.post("/api/llm/providers", headers=as_(corpus, "admin"), json={
        "name": f"openai-{uuid.uuid4().hex[:6]}", "provider_type": "openai",
        "credentials": {"api_key": "sk-test-not-used"},
        "models": [{"model_id": "gpt-6-luna", "name": "GPT-6 Luna", "is_custom": False, "is_default": True}],
    })
    assert r.status_code == 200, r.text

    async def fake_main_execution(self):
        from app.models.completion_block import CompletionBlock
        from app.services.agent.agent_execution_service import AgentExecutionService

        await AgentExecutionService().start_run(
            self.db, completion_id=str(self.system_completion.id), organization_id=str(self.organization.id),
            user_id=str(self.system_completion.user_id) if self.system_completion.user_id else None,
            report_id=str(self.report.id))
        self.system_completion.status = "success"
        self.system_completion.completion = {"content": "Rock leads."}
        self.db.add(self.system_completion)
        self.db.add(CompletionBlock(completion_id=str(self.system_completion.id), source_type="final",
                                    block_index=0, title="Answer", status="completed", content="Rock leads."))
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _embed(test_client, corpus, aid, name="Back office"):
    admin = as_(corpus, "admin")
    membership = corpus.cast.people["member"].membership_id
    test_client.put(f"/api/organizations/{corpus.cast.org_id}/members/{membership}",
                    json={"custom_attributes": {"employee_id": "E-90"}}, headers=admin)
    host = test_client.post("/api/host_applications", json={
        "name": name, "allowed_origins": ["https://erp.example.com"], "identity_mode": "assertion",
        "identity_source": "member_attribute:employee_id", "issuer": "erp"}, headers=admin).json()
    assert test_client.put(f"/api/artifacts/{aid}/embedding", json={"host_application_ids": [host["id"]]},
                           headers=as_(corpus, "owner")).status_code == 200
    now = int(time.time())
    assertion = jwt.encode({"iss": "erp", "aud": host["public_id"], "sub": "E-90", "iat": now, "exp": now + 60,
                            "jti": uuid.uuid4().hex}, host["private_key"], algorithm="RS256")
    token = test_client.post(f"/api/embed/{host['public_id']}/session", json={"assertion": assertion}).json()["token"]
    return host, {"Authorization": f"Bearer {token}"}


def _talk(test_client, aid, headers, text="hi"):
    conv = test_client.post(base(aid), json={}, headers=headers)
    assert conv.status_code == 201, conv.text
    sent = test_client.post(f"{base(aid)}/{conv.json()['id']}/messages", json={"text": text}, headers=headers)
    assert sent.status_code == 200, sent.text
    return conv.json()["id"]


def _my_reports(test_client, headers):
    r = test_client.get("/api/reports?filter=my&limit=50", headers=headers)
    assert r.status_code == 200, r.text
    return {x["id"]: x for x in r.json()["reports"]}


def test_embedded_conversations_list_with_their_host_app_and_runs_name_it(corpus, corpus_rows, test_client,
                                                                        stub_agent):
    aid = _chatty(corpus, corpus_rows, test_client)
    host, embed = _embed(test_client, corpus, aid)
    in_host = _talk(test_client, aid, embed)
    in_bow = _talk(test_client, aid, as_(corpus, "member"))  # the same app, inside BOW

    mine = _my_reports(test_client, as_(corpus, "member"))
    assert mine[in_host]["origin_app"] == {"type": "host_app", "id": host["id"], "name": "Back office"}
    assert mine[in_host]["report_type"] == "artifact_chat"
    assert in_bow not in mine
    assert all(r["origin_app"] is None for rid, r in mine.items() if rid != in_host)
    # Only its owner lists it, and opening it clears its unread dot.
    assert in_host not in _my_reports(test_client, as_(corpus, "owner"))
    assert test_client.post(f"/api/reports/{in_host}/viewed", headers=as_(corpus, "member")).status_code == 200

    # Renamed: named as it is called now.
    test_client.patch(f"/api/host_applications/{host['id']}", json={"revision": host["revision"], "name": "Priority"},
                      headers=as_(corpus, "admin"))
    assert _my_reports(test_client, as_(corpus, "member"))[in_host]["origin_app"]["name"] == "Priority"

    runs = test_client.get("/api/console/diagnosis/runs?limit=50", headers=as_(corpus, "admin"))
    assert runs.status_code == 200, runs.text
    by_report = {}
    for item in runs.json()["items"]:
        by_report.setdefault(item["report"]["id"], []).append(item)
    assert [i["origin_app"]["name"] for i in by_report[in_host]] == ["Priority"]
    assert [i["origin_app"] for i in by_report[in_bow]] == [None]


def test_a_conversation_started_with_an_oauth_app_is_named_after_it(corpus, test_client, create_oauth_client):
    from urllib.parse import parse_qs, urlparse
    import base64
    import hashlib

    admin = as_(corpus, "admin")
    login = admin["Authorization"].split(" ", 1)[1]
    redirect, verifier = "http://localhost:4173/callback", "d" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    app = create_oauth_client(login, corpus.cast.org_id, name="Claude Desktop", scopes="app",
                              redirect_uris=[redirect])
    approved = test_client.post("/api/oauth/authorize", headers=admin, json={
        "client_id": app["client_id"], "redirect_uri": redirect, "scope": "app", "code_challenge": challenge})
    assert approved.status_code == 200, approved.text
    code = parse_qs(urlparse(approved.json()["redirect_url"]).query)["code"][0]
    access = test_client.post("/api/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "client_id": app["client_id"],
        "code_verifier": verifier, "redirect_uri": redirect}).json()["access_token"]

    via_app = {"Authorization": f"Bearer {access}", "X-Organization-Id": corpus.cast.org_id}
    made = test_client.post("/api/reports", json={"title": "From the desktop app"}, headers=via_app)
    assert made.status_code == 200, made.text
    in_bow = test_client.post("/api/reports", json={"title": "In BOW"}, headers=admin).json()

    mine = _my_reports(test_client, admin)
    assert mine[made.json()["id"]]["origin_app"]["type"] == "oauth_app"
    assert mine[made.json()["id"]]["origin_app"]["name"] == "Claude Desktop"
    assert mine[in_bow["id"]]["origin_app"] is None
