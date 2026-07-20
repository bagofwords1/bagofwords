"""Completion model-id resolution contract.

Regression guard for the "start a chat from the home page → first message
fails with HTTP 400, retry works" bug. The home PromptBoxV2 defaults to the
``auto`` model sentinel when the org's Auto router is on, and ``createReport``
forwarded that sentinel verbatim as ``prompt.model_id='auto'`` (the in-report
submit path already mapped it to ``null``). ``auto`` is not a real model id, so
the backend rejected the first completion with 400.

The invariant this locks in, stated generally (not just for the reported
``auto`` value): creating a completion with a **model_id the org does not own**
is rejected with 400, while **omitting model_id** (what the corrected frontend
sends — sentinel mapped to null) resolves the org default and starts the run.

The agent loop is stubbed at ``AgentV2.main_execution`` so no LLM is contacted;
routes/services/DB run real.
"""
import json
import uuid

import pytest

from app.ai.agent_v2 import AgentV2


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _stub_success(monkeypatch):
    """Agent completes instantly and successfully, no LLM involved."""
    async def fake_main_execution(self):
        self.system_completion.status = "success"
        self.db.add(self.system_completion)
        await self.db.commit()
    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)


def _create_completion(test_client, report_id, token, org_id, *, model_id=...):
    prompt = {"content": "hi", "mentions": [{}]}
    # model_id=... (Ellipsis) means "omit the field entirely" (the corrected
    # frontend payload for Auto); an explicit value is sent through as-is.
    if model_id is not ...:
        prompt["model_id"] = model_id
    return test_client.post(
        f"/api/reports/{report_id}/completions?background=true",
        json={"prompt": prompt},
        headers=_headers(token, org_id),
    )


@pytest.mark.e2e
def test_completion_resolves_default_when_model_id_omitted(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(token, org_id)
    _stub_success(monkeypatch)
    report = create_report(title=f"model-omit-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])

    # This is exactly what the fixed home-page flow sends (Auto sentinel → null,
    # i.e. no model_id field): the org default must resolve and the run starts.
    r = _create_completion(test_client, report["id"], token, org_id, model_id=...)
    assert r.status_code == 200, r.json()
    # The default resolved and a system run was started (not rejected). The
    # background run finishes asynchronously, so the snapshot may still read
    # in_progress — the invariant is "started, not 400", not the final status.
    system = next(c for c in r.json()["completions"] if c["role"] == "system")
    assert system["status"] in ("in_progress", "success")


@pytest.mark.e2e
@pytest.mark.parametrize(
    "bad_model_id",
    [
        "auto",                       # the exact sentinel the home box leaked
        str(uuid.uuid4()),            # a well-formed id the org does not own
        "definitely-not-a-model",     # arbitrary garbage
    ],
)
def test_completion_rejects_unknown_model_id(
    bad_model_id,
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models,
):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(token, org_id)
    _stub_success(monkeypatch)
    report = create_report(title=f"model-bad-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])

    # Any model_id the org can't resolve is a 400 (not a 500, not a silent
    # fallback). This is why the sentinel must never leave the frontend.
    r = _create_completion(test_client, report["id"], token, org_id, model_id=bad_model_id)
    assert r.status_code == 400, r.json()
    assert "model" in r.json()["detail"].lower()


@pytest.mark.e2e
def test_completion_started_event_carries_resolved_model(
    monkeypatch, test_client, create_report, create_user, login_user, whoami,
    create_llm_provider_and_models, get_default_model, create_completion_stream,
):
    """The streaming ``completion.started`` event carries the resolved model id.

    The report page's optimistic assistant bubble is created with
    ``model=null`` when Auto/the router is selected, so the model badge on the
    avatar only appeared after a full reload. The live message now stamps its
    model from this event's payload — so the event must include a non-empty
    ``model`` equal to the model that will actually run.
    """
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    create_llm_provider_and_models(token, org_id)
    default_model = get_default_model(token, org_id)
    assert len(default_model) == 1
    expected_model_id = default_model[0]["model_id"]
    _stub_success(monkeypatch)
    report = create_report(title=f"started-model-{uuid.uuid4().hex[:8]}", user_token=token, org_id=org_id, data_sources=[])

    lines = create_completion_stream(
        report_id=report["id"], prompt="hi", user_token=token, org_id=org_id,
    )
    started_model = ...  # sentinel: event not seen yet
    current_event = None
    for raw in lines:
        line = raw.decode() if isinstance(raw, (bytes, bytearray)) else raw
        if not line or line.startswith(":"):
            continue
        if line.startswith("event:"):
            current_event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and current_event == "completion.started":
            obj = json.loads(line.split(":", 1)[1].strip())
            # The SSE frame nests the payload under `data` (the client reads
            # `parsed.data ?? parsed`); tolerate either shape.
            payload = obj.get("data", obj)
            started_model = payload.get("model")
        if line.strip() == "data: [DONE]":
            break

    assert started_model is not ..., "no completion.started event was emitted"
    # Resolved and non-empty — an empty/None model is exactly what left the live
    # badge blank. It must match the model the run resolved to.
    assert started_model
    assert started_model == expected_model_id
