"""Reasoning effort through the API: capability, admin settings, persistence.

The level a user picks is stored beside every model override (reports,
triggers, saved prompts, scheduled prompts, eval cases) and validated on
write; admins shape how a model receives it. No LLM call is made — every
assertion goes through route → service → DB.
"""
import uuid

import pytest


def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def cast(test_client, bootstrap_admin):
    admin = bootstrap_admin("effort")
    t, org = admin["token"], admin["org_id"]
    suffix = uuid.uuid4().hex[:6]
    opaque_id = f"prod-deploy-{suffix}"
    resp = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"prov-{suffix}",
            "provider_type": "anthropic",
            "credentials": {"api_key": "dummy-key"},
            "models": [
                {"model_id": "claude-sonnet-5", "name": "Claude Sonnet 5", "is_enabled": True},
                {"model_id": opaque_id, "name": "Opaque deployment", "is_custom": True, "is_enabled": True},
            ],
        },
        headers=_headers(t, org),
    )
    assert resp.status_code == 200, resp.json()
    models = test_client.get("/api/llm/models", headers=_headers(t, org)).json()
    by_id = {m["model_id"]: m for m in models}
    return {"token": t, "org_id": org, "known": by_id["claude-sonnet-5"], "opaque": by_id[opaque_id]}


def _models(test_client, cast):
    resp = test_client.get("/api/llm/models", headers=_headers(cast["token"], cast["org_id"]))
    assert resp.status_code == 200
    return {m["id"]: m for m in resp.json()}


def _set_reasoning(test_client, token, org_id, model_db_id, body):
    return test_client.post(f"/api/llm/models/{model_db_id}/reasoning", json=body, headers=_headers(token, org_id))


@pytest.mark.e2e
def test_models_list_reports_what_each_level_runs_as(test_client, cast):
    known, opaque = cast["known"]["reasoning"], cast["opaque"]["reasoning"]
    assert known["supported"] is True
    assert set(known["levels"]) == {"low", "medium", "high", "max"}
    assert all(v in known["efforts"] for v in known["levels"].values())
    # An unknown deployment name does not claim a capability it may not have.
    assert opaque["supported"] is False


@pytest.mark.e2e
def test_admin_reasoning_settings_persist_and_change_capability(test_client, cast):
    t, org, opaque = cast["token"], cast["org_id"], cast["opaque"]
    r = _set_reasoning(test_client, t, org, opaque["id"], {
        "mode": "like", "like_model_id": "gpt-5.4", "default_effort": "High",
        "params": {"high": {"text": {"verbosity": "low"}}},
    })
    assert r.status_code == 200, r.json()
    info = _models(test_client, cast)[opaque["id"]]["reasoning"]
    assert info["supported"] and info["mode"] == "like"
    assert info["levels"]["max"] == "xhigh"          # gpt-5.4 has no "max"
    assert info["default"] == "high"
    assert info["params"] == {"high": {"text": {"verbosity": "low"}}}
    # Other config keys survive (reasoning writes merge, never replace).
    assert test_client.post(f"/api/llm/models/{opaque['id']}/set_temperature", params={"temperature": 0.5},
                            headers=_headers(t, org)).status_code == 200
    assert _set_reasoning(test_client, t, org, opaque["id"], {"default_effort": None}).status_code == 200
    m = _models(test_client, cast)[opaque["id"]]
    assert m["config"]["temperature"] == 0.5 and m["reasoning"]["default"] is None
    assert m["reasoning"]["mode"] == "like"          # omitted fields unchanged
    # Back to automatic: an unknown id offers only the levels the admin wrote
    # raw fields for; clearing them drops the capability entirely.
    assert _set_reasoning(test_client, t, org, opaque["id"], {"mode": "auto"}).status_code == 200
    info = _models(test_client, cast)[opaque["id"]]["reasoning"]
    assert info["supported"] and set(info["levels"].values()) == {"high"}
    assert _set_reasoning(test_client, t, org, opaque["id"], {"params": None}).status_code == 200
    assert _models(test_client, cast)[opaque["id"]]["reasoning"]["supported"] is False


@pytest.mark.e2e
@pytest.mark.parametrize("body", [
    {"mode": "turbo"},
    {"default_effort": "ultra"},
    {"params": {"extreme": {"a": 1}}},
    {"params": {"high": {"messages": []}}},        # would replace the conversation
    {"params": {"high": {"model": "other"}}},      # would swap the model
])
def test_admin_reasoning_settings_reject_bad_input(test_client, cast, body):
    r = _set_reasoning(test_client, cast["token"], cast["org_id"], cast["opaque"]["id"], body)
    assert r.status_code == 422


@pytest.mark.e2e
def test_custom_and_like_modes_need_their_inputs(test_client, cast):
    t, org, mid = cast["token"], cast["org_id"], cast["opaque"]["id"]
    assert _set_reasoning(test_client, t, org, mid, {"mode": "custom"}).status_code == 400
    assert _set_reasoning(test_client, t, org, mid, {"mode": "like"}).status_code == 400
    r = _set_reasoning(test_client, t, org, mid, {"mode": "custom", "params": {"max": {"think": "high"}}})
    assert r.status_code == 200
    assert r.json()["reasoning"]["levels"] == {"low": "max", "medium": "max", "high": "max", "max": "max"}


@pytest.mark.e2e
def test_reasoning_settings_require_manage_llm(test_client, cast, invite_user_to_org):
    member = invite_user_to_org(org_id=cast["org_id"], admin_token=cast["token"])
    r = _set_reasoning(test_client, member["token"], cast["org_id"], cast["known"]["id"], {"default_effort": "high"})
    assert r.status_code == 403
    r = test_client.post(f"/api/llm/models/{cast['known']['id']}/test_reasoning", json={"effort": "high"},
                         headers=_headers(member["token"], cast["org_id"]))
    assert r.status_code == 403
    # Members still see the capability they pick from.
    models = test_client.get("/api/llm/models", headers=_headers(member["token"], cast["org_id"])).json()
    assert any(m["reasoning"]["supported"] for m in models)


@pytest.mark.e2e
def test_report_level_is_stored_beside_the_model(test_client, cast):
    t, org = cast["token"], cast["org_id"]
    h = _headers(t, org)
    created = test_client.post("/api/reports", json={
        "title": "effort", "files": [], "data_sources": [],
        "model_id": cast["known"]["id"], "reasoning_effort": "HIGH",
    }, headers=h)
    assert created.status_code == 200, created.json()
    rid = created.json()["id"]
    assert test_client.get(f"/api/reports/{rid}", headers=h).json()["reasoning_effort"] == "high"
    # Omitted = unchanged; a value sets; "" clears back to Default.
    assert test_client.put(f"/api/reports/{rid}", json={"title": "renamed"}, headers=h).status_code == 200
    assert test_client.get(f"/api/reports/{rid}", headers=h).json()["reasoning_effort"] == "high"
    assert test_client.put(f"/api/reports/{rid}", json={"reasoning_effort": "max"}, headers=h).status_code == 200
    assert test_client.get(f"/api/reports/{rid}", headers=h).json()["reasoning_effort"] == "max"
    assert test_client.put(f"/api/reports/{rid}", json={"reasoning_effort": ""}, headers=h).status_code == 200
    assert test_client.get(f"/api/reports/{rid}", headers=h).json()["reasoning_effort"] is None
    assert test_client.put(f"/api/reports/{rid}", json={"reasoning_effort": "ultra"}, headers=h).status_code == 422


@pytest.mark.e2e
def test_turn_rejects_an_unknown_level(test_client, cast):
    h = _headers(cast["token"], cast["org_id"])
    rid = test_client.post("/api/reports", json={"title": "x", "files": [], "data_sources": []}, headers=h).json()["id"]
    r = test_client.post(f"/api/reports/{rid}/completions",
                         json={"prompt": {"content": "hi", "reasoning_effort": "ultra"}}, headers=h)
    assert r.status_code == 422


@pytest.mark.e2e
def test_saved_prompt_and_trigger_keep_their_level(test_client, cast):
    h = _headers(cast["token"], cast["org_id"])
    p = test_client.post("/api/prompts", json={
        "title": "weekly", "text": "summarize sales", "scope": "private",
        "model_id": cast["known"]["id"], "reasoning_effort": "max",
    }, headers=h)
    assert p.status_code == 200, p.json()
    assert p.json()["reasoning_effort"] == "max"
    updated = test_client.put(f"/api/prompts/{p.json()['id']}", json={"reasoning_effort": None}, headers=h)
    assert updated.status_code == 200 and updated.json()["reasoning_effort"] is None

    trig = test_client.post("/api/triggers", json={
        "name": "on push", "task_template": "review", "is_active": False,
        "model_id": cast["known"]["id"], "reasoning_effort": "low",
    }, headers=h)
    assert trig.status_code == 200, trig.json()
    assert trig.json()["reasoning_effort"] == "low"
    moved = test_client.put(f"/api/triggers/{trig.json()['id']}", json={"reasoning_effort": "high"}, headers=h)
    assert moved.status_code == 200 and moved.json()["reasoning_effort"] == "high"
    bad = test_client.put(f"/api/triggers/{trig.json()['id']}", json={"reasoning_effort": "ultra"}, headers=h)
    assert bad.status_code == 422


@pytest.mark.e2e
def test_scheduled_prompt_level_is_validated_and_normalized(test_client, cast):
    h = _headers(cast["token"], cast["org_id"])
    rid = test_client.post("/api/reports", json={"title": "s", "files": [], "data_sources": []}, headers=h).json()["id"]
    body = {"prompt": {"content": "daily", "reasoning_effort": "MAX"}, "cron_schedule": "0 8 * * *", "is_active": False}
    r = test_client.post(f"/api/reports/{rid}/scheduled-prompts", json=body, headers=h)
    assert r.status_code == 200, r.json()
    assert r.json()["prompt"]["reasoning_effort"] == "max"
    body["prompt"]["reasoning_effort"] = "ultra"
    assert test_client.post(f"/api/reports/{rid}/scheduled-prompts", json=body, headers=h).status_code == 422
