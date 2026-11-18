"""Starting an eval run returns the created run, build included.

Every run is stamped with the org's main build. Serializing that run used to
lazy-load its `build` relationship outside the async context, so the endpoints
answered 500 even though the run had been committed — the UI showed "failed"
and a retry started a second run.
"""
import pytest


def _configure_default_model(test_client, headers):
    # Starting a run stubs a report that needs a default model. Nothing here
    # calls the model — execution starts only when a client streams the run —
    # so a registered provider with a placeholder endpoint is enough.
    provider = test_client.post("/api/llm/providers", json={
        "name": "Placeholder", "provider_type": "custom",
        "credentials": {"base_url": "http://127.0.0.1:9/v1", "api_key": "sk-placeholder"},
        "models": [{"model_id": "placeholder-model", "name": "Placeholder", "is_custom": True, "is_enabled": True}],
    }, headers=headers)
    assert provider.status_code == 200, provider.text
    models = test_client.get("/api/llm/models", headers=headers).json()
    model = next(m for m in models if m.get("model_id") == "placeholder-model")
    assert test_client.post(f"/api/llm/models/{model['id']}/set_default", headers=headers).status_code == 200


@pytest.fixture
def org_with_main_build(test_client, create_user, login_user, whoami, create_global_instruction, get_main_build,
                        create_test_suite, create_test_case):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    # A published instruction is what gives the org a main build.
    create_global_instruction(text="Answer in one sentence", user_token=token, org_id=org_id, status="published")
    build = get_main_build(user_token=token, org_id=org_id)
    assert build is not None
    suite = create_test_suite(name="Run creation", user_token=token, org_id=org_id)
    case = create_test_case(suite_id=suite["id"], name="Any case", user_token=token, org_id=org_id)
    headers = {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}
    _configure_default_model(test_client, headers)
    return {"headers": headers, "build": build, "suite": suite, "case": case}


def _assert_run_on_build(response, build):
    assert response.status_code == 200, response.text
    run = response.json()
    assert run["id"]
    assert run["build_id"] == build["id"]
    assert run["build_number"] == build["build_number"]


@pytest.mark.e2e
@pytest.mark.parametrize("pin_build", [False, True], ids=["main-build-resolved", "build-pinned"])
def test_starting_a_run_for_cases_returns_the_run(test_client, org_with_main_build, pin_build):
    ctx = org_with_main_build
    body = {"case_ids": [ctx["case"]["id"]], "trigger_reason": "manual"}
    if pin_build:
        body["build_id"] = ctx["build"]["id"]
    response = test_client.post("/api/tests/runs", json=body, headers=ctx["headers"])
    _assert_run_on_build(response, ctx["build"])


@pytest.mark.e2e
def test_starting_a_suite_run_returns_the_run(test_client, org_with_main_build):
    ctx = org_with_main_build
    response = test_client.post(f"/api/tests/suites/{ctx['suite']['id']}/runs", headers=ctx["headers"])
    _assert_run_on_build(response, ctx["build"])


@pytest.mark.e2e
def test_a_started_run_is_listed_once(test_client, org_with_main_build):
    ctx = org_with_main_build
    response = test_client.post(
        "/api/tests/runs", json={"case_ids": [ctx["case"]["id"]], "trigger_reason": "manual"}, headers=ctx["headers"]
    )
    _assert_run_on_build(response, ctx["build"])
    runs = test_client.get(f"/api/tests/runs?suite_id={ctx['suite']['id']}", headers=ctx["headers"]).json()
    assert [r["id"] for r in runs] == [response.json()["id"]]
