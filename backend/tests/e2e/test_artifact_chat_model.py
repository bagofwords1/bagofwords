"""Default model for chat on the shared artifact page /r/{id}.

The owner picks it in the share dialog (artifact_chat_model_id on
PUT /reports/{id}/visibility/artifact). Contract locked in here:

- round-trips through GET /reports/{id}; "" clears it; omitting the field
  leaves it unchanged; an unknown model id is rejected.
- every viewer message re-syncs the viewer's hidden chat report
  (report_type='artifact_chat') to the setting — including chat reports that
  already existed before the owner changed it — falling back to the
  dashboard's own model_id when the setting is cleared, and pinning nothing
  (organization default at run time) for "org_default".

The agent loop is stubbed at AgentV2.main_execution so no LLM is contacted;
routes/services/DB run real.
"""
import uuid

import pytest

from app.ai.agent_v2 import AgentV2


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _stub_success(monkeypatch):
    async def fake_main_execution(self):
        self.system_completion.status = "success"
        self.db.add(self.system_completion)
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _put_chat(test_client, report_id, token, org_id, **body):
    return test_client.put(
        f"/api/reports/{report_id}/visibility/artifact",
        json={"visibility": "internal", **body},
        headers=_headers(token, org_id),
    )


def _setup(test_client, create_user, login_user, whoami, create_llm_provider_and_models, create_report):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(token, org_id)
    models = test_client.get("/api/llm/models?is_enabled=true", headers=_headers(token, org_id)).json()
    assert len(models) >= 2
    report = create_report(title=f"chat-model-{uuid.uuid4().hex[:6]}", user_token=token, org_id=org_id, data_sources=[])
    return token, org_id, models, report


def _send_chat(test_client, report_id, token, org_id):
    r = test_client.post(
        f"/api/r/{report_id}/chat/completions",
        json={"prompt": {"content": "hi", "mentions": [{}]}},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200, r.text


def _chat_report_model(test_client, report_id, token, org_id):
    status = test_client.get(f"/api/r/{report_id}/chat", headers=_headers(token, org_id)).json()
    chat_report_id = status["chat_report_id"]
    assert chat_report_id
    return test_client.get(f"/api/reports/{chat_report_id}", headers=_headers(token, org_id)).json()["model_id"]


@pytest.mark.e2e
def test_chat_model_set_clear_and_omit(
    monkeypatch, test_client, create_user, login_user, whoami,
    create_llm_provider_and_models, create_report, get_report,
):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    token, org_id, models, report = _setup(
        test_client, create_user, login_user, whoami, create_llm_provider_and_models, create_report,
    )
    rid = report["id"]

    # Unset by default.
    assert get_report(rid, user_token=token, org_id=org_id)["artifact_chat_model_id"] is None

    r = _put_chat(test_client, rid, token, org_id, artifact_chat_enabled=True, artifact_chat_model_id=models[0]["id"])
    assert r.status_code == 200, r.json()
    assert r.json()["artifact_chat_model_id"] == models[0]["id"]
    assert get_report(rid, user_token=token, org_id=org_id)["artifact_chat_model_id"] == models[0]["id"]

    # Unrelated edits (the dialog resends visibility) leave it alone.
    assert _put_chat(test_client, rid, token, org_id, include_data_tab=False).status_code == 200
    assert _put_chat(test_client, rid, token, org_id, artifact_chat_data_source_ids=[]).status_code == 200
    assert get_report(rid, user_token=token, org_id=org_id)["artifact_chat_model_id"] == models[0]["id"]

    # Unknown model is rejected and the stored value survives.
    r = _put_chat(test_client, rid, token, org_id, artifact_chat_model_id=str(uuid.uuid4()))
    assert r.status_code == 404
    assert get_report(rid, user_token=token, org_id=org_id)["artifact_chat_model_id"] == models[0]["id"]

    # "org_default" is stored as-is (no model validation — it is not an id).
    assert _put_chat(test_client, rid, token, org_id, artifact_chat_model_id="org_default").status_code == 200
    assert get_report(rid, user_token=token, org_id=org_id)["artifact_chat_model_id"] == "org_default"

    # "" clears back to inherit.
    assert _put_chat(test_client, rid, token, org_id, artifact_chat_model_id="").status_code == 200
    assert get_report(rid, user_token=token, org_id=org_id)["artifact_chat_model_id"] is None


@pytest.mark.e2e
def test_chat_model_ignored_on_conversation_share(
    monkeypatch, test_client, create_user, login_user, whoami,
    create_llm_provider_and_models, create_report, get_report,
):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    token, org_id, models, report = _setup(
        test_client, create_user, login_user, whoami, create_llm_provider_and_models, create_report,
    )
    r = test_client.put(
        f"/api/reports/{report['id']}/visibility/conversation",
        json={"visibility": "internal", "artifact_chat_model_id": models[0]["id"]},
        headers=_headers(token, org_id),
    )
    assert r.status_code == 200
    assert get_report(report["id"], user_token=token, org_id=org_id)["artifact_chat_model_id"] is None


@pytest.mark.e2e
def test_chat_report_model_follows_owner_setting(
    monkeypatch, test_client, create_user, login_user, whoami,
    create_llm_provider_and_models, create_report,
):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    _stub_success(monkeypatch)
    token, org_id, models, report = _setup(
        test_client, create_user, login_user, whoami, create_llm_provider_and_models, create_report,
    )
    rid = report["id"]
    m1, m2 = models[0]["id"], models[1]["id"]

    assert _put_chat(test_client, rid, token, org_id, artifact_chat_enabled=True,
                     artifact_chat_model_id=m1).status_code == 200
    _send_chat(test_client, rid, token, org_id)
    assert _chat_report_model(test_client, rid, token, org_id) == m1

    # Changing the setting reaches the EXISTING chat report on the next message.
    assert _put_chat(test_client, rid, token, org_id, artifact_chat_model_id=m2).status_code == 200
    _send_chat(test_client, rid, token, org_id)
    assert _chat_report_model(test_client, rid, token, org_id) == m2

    # Cleared → inherits the dashboard's own model override.
    r = test_client.put(f"/api/reports/{rid}", json={"model_id": m1}, headers=_headers(token, org_id))
    assert r.status_code == 200, r.json()
    assert _put_chat(test_client, rid, token, org_id, artifact_chat_model_id="").status_code == 200
    _send_chat(test_client, rid, token, org_id)
    assert _chat_report_model(test_client, rid, token, org_id) == m1

    # "org_default" skips the dashboard's model: nothing pinned, so the
    # organization default resolves at run time.
    assert _put_chat(test_client, rid, token, org_id, artifact_chat_model_id="org_default").status_code == 200
    _send_chat(test_client, rid, token, org_id)
    assert _chat_report_model(test_client, rid, token, org_id) is None
    assert _put_chat(test_client, rid, token, org_id, artifact_chat_model_id="").status_code == 200

    # No dashboard model either → nothing pinned (viewer/org default at run time).
    assert test_client.put(f"/api/reports/{rid}", json={"model_id": ""},
                           headers=_headers(token, org_id)).status_code == 200
    _send_chat(test_client, rid, token, org_id)
    assert _chat_report_model(test_client, rid, token, org_id) is None
