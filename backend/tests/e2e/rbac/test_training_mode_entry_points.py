"""Every way of starting a training turn goes through the training gate.

The gate (``ReportService._assert_can_enter_training_mode``) needs the org
flag plus ``manage_instructions`` on every agent the report is attached to.
``test_rbac_training_mode.py`` covers it on report create and update. This
file covers the two side doors:

- ``POST /prompts/{id}/run``, which starts a report in the prompt's stored mode
- a completion payload carrying ``mode: training`` on a chat report, since
  the agent reads the payload's mode before the report's

World: ``u`` only views agent1 and manages agent2.
"""
import time
import uuid

import pytest

from app.ai.agent_v2 import AgentV2


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def world(test_client, bootstrap_admin, invite_user_to_org, grant_resource, sqlite_data_source):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    agent1 = sqlite_data_source(name=f"te1_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    agent2 = sqlite_data_source(name=f"te2_{uuid.uuid4().hex[:6]}", user_token=admin["token"], org_id=org_id)
    u = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    for ds, perms in ((agent1, ["view"]), (agent2, ["manage"])):
        r = grant_resource(
            resource_type="data_source", resource_id=ds["id"],
            principal_type="user", principal_id=u["user_id"],
            permissions=perms, user_token=admin["token"], org_id=org_id,
        )
        assert r.status_code == 200, r.text
    return {"org_id": org_id, "admin": admin, "u": u, "agent1": agent1, "agent2": agent2}


def _report_ids(test_client, user, org_id):
    r = test_client.get("/api/reports", params={"filter": "my", "limit": 100}, headers=_hdr(user["token"], org_id))
    assert r.status_code == 200, r.text
    return {rep["id"] for rep in r.json()["reports"]}


@pytest.fixture
def stub_completion(monkeypatch):
    """Prompt-run starts a completion. Only the report it creates matters here."""
    from app.services.completion_service import CompletionService

    async def _fake(self, db, report_id, *a, **kw):
        return {"stubbed": True}
    monkeypatch.setattr(CompletionService, "create_completion", _fake)


@pytest.mark.e2e
def test_prompt_run_applies_the_training_gate(test_client, world, stub_completion):
    org_id, admin, u = world["org_id"], world["admin"], world["u"]

    def _training_prompt(ds_id):
        r = test_client.post(
            "/api/prompts",
            json={"text": "train me", "scope": "agent", "data_source_ids": [ds_id], "mode": "training"},
            headers=_hdr(admin["token"], org_id),
        )
        assert r.status_code == 200, r.text
        return r.json()["id"]

    # Viewer of agent1: they can see and run the prompt, but not as training.
    before = _report_ids(test_client, u, org_id)
    denied = test_client.post(f"/api/prompts/{_training_prompt(world['agent1']['id'])}/run",
                              json={}, headers=_hdr(u["token"], org_id))
    assert denied.status_code == 403, denied.text
    assert _report_ids(test_client, u, org_id) == before, "a denied run must leave no report behind"

    # Manager of agent2: allowed, and the report really is in training mode.
    ok = test_client.post(f"/api/prompts/{_training_prompt(world['agent2']['id'])}/run",
                          json={}, headers=_hdr(u["token"], org_id))
    assert ok.status_code == 200, ok.text
    rep = test_client.get(f"/api/reports/{ok.json()['report_id']}", headers=_hdr(u["token"], org_id))
    assert rep.json()["mode"] == "training"


@pytest.fixture
def agent_modes(monkeypatch, test_client, world, create_llm_provider_and_models):
    """Record the mode each agent run starts in. Stubs the LLM loop only."""
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    create_llm_provider_and_models(world["admin"]["token"], world["org_id"])
    seen = []

    async def _fake_main_execution(self):
        seen.append(self.mode)
        self.system_completion.status = "success"
        self.db.add(self.system_completion)
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", _fake_main_execution)
    return seen


def _run_turn(test_client, user, org_id, report_id, mode, seen):
    n = len(seen)
    r = test_client.post(
        f"/api/reports/{report_id}/completions",
        json={"prompt": {"content": "hi", "mentions": [{}], "mode": mode}},
        headers=_hdr(user["token"], org_id),
    )
    assert r.status_code == 200, r.text
    deadline = time.time() + 15
    while len(seen) == n and time.time() < deadline:
        time.sleep(0.2)
    assert len(seen) > n, "agent never ran"
    return seen[-1]


@pytest.mark.e2e
def test_completion_payload_cannot_switch_a_chat_report_into_training(test_client, world, agent_modes):
    org_id, u = world["org_id"], world["u"]

    def _report(ds_id):
        r = test_client.post("/api/reports", json={"title": "t", "data_sources": [ds_id]},
                             headers=_hdr(u["token"], org_id))
        assert r.status_code in (200, 201), r.text
        return r.json()["id"]

    # Chat report on an agent u only views: asking for training runs chat.
    chat = _report(world["agent1"]["id"])
    assert _run_turn(test_client, u, org_id, chat, "training", agent_modes) == "chat"

    # A report that legitimately passed the gate still runs training.
    trained = _report(world["agent2"]["id"])
    r = test_client.put(f"/api/reports/{trained}", json={"mode": "training"}, headers=_hdr(u["token"], org_id))
    assert r.status_code == 200, r.text
    assert _run_turn(test_client, u, org_id, trained, "training", agent_modes) == "training"
