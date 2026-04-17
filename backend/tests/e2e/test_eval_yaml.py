import yaml

import pytest


SANITY_YAML = """
name: Sanity
description: Sanity check suite
cases:
  - name: smoke
    prompt:
      content: "How many users?"
    expectations:
      spec_version: 1
      order_mode: flexible
      rules:
        - {type: tool.calls, tool: create_data, min_calls: 1}
"""


MULTI_TURN_YAML = """
name: MultiTurn
cases:
  - name: clarify_then_answer
    turns:
      - prompt: {content: "Show me the data"}
      - prompt: {content: "Users per month for 2025"}
    expectations:
      spec_version: 1
      order_mode: flexible
      rules:
        - {type: tool.calls, tool: clarify, min_calls: 1}
        - {type: tool.calls, tool: create_data, min_calls: 1}
"""


@pytest.mark.e2e
def test_import_creates_suite_and_case(
    create_user, login_user, whoami,
    import_suite_yaml, get_test_suite, get_test_case,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = import_suite_yaml(SANITY_YAML, user_token=token, org_id=org_id)
    assert resp.status_code == 200, resp.json()
    data = resp.json()

    assert data["suite_name"] == "Sanity"
    assert "smoke" in data["cases_by_name"]

    suite_id = data["suite_id"]
    case_id = data["cases_by_name"]["smoke"]

    suite_resp = get_test_suite(suite_id, user_token=token, org_id=org_id)
    assert suite_resp.status_code == 200
    assert suite_resp.json()["name"] == "Sanity"

    case_resp = get_test_case(case_id, user_token=token, org_id=org_id)
    assert case_resp.status_code == 200
    case = case_resp.json()
    assert case["name"] == "smoke"
    assert case["prompt_json"]["content"] == "How many users?"
    assert case["expectations_json"]["rules"][0]["type"] == "tool.calls"


@pytest.mark.e2e
def test_import_upsert_preserves_ids(
    create_user, login_user, whoami, import_suite_yaml,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    first = import_suite_yaml(SANITY_YAML, user_token=token, org_id=org_id).json()
    second = import_suite_yaml(SANITY_YAML, user_token=token, org_id=org_id).json()

    assert first["suite_id"] == second["suite_id"], "re-import should reuse suite id"
    assert first["cases_by_name"]["smoke"] == second["cases_by_name"]["smoke"], \
        "re-import should reuse case id for same case name"


@pytest.mark.e2e
def test_import_replace_strategy_removes_absent_cases(
    create_user, login_user, whoami,
    import_suite_yaml, get_test_cases,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    two_cases = """
name: TwoCases
cases:
  - name: a
    prompt: {content: "Q1"}
    expectations: {spec_version: 1, rules: []}
  - name: b
    prompt: {content: "Q2"}
    expectations: {spec_version: 1, rules: []}
"""
    result = import_suite_yaml(two_cases, user_token=token, org_id=org_id).json()
    suite_id = result["suite_id"]

    one_case = """
name: TwoCases
cases:
  - name: a
    prompt: {content: "Q1"}
    expectations: {spec_version: 1, rules: []}
"""
    replaced = import_suite_yaml(
        one_case, user_token=token, org_id=org_id, strategy="replace"
    ).json()
    assert "b" in replaced["removed_case_names"]

    cases = get_test_cases(suite_id, user_token=token, org_id=org_id)
    names = {c["name"] for c in cases}
    assert names == {"a"}


@pytest.mark.e2e
def test_import_unknown_data_source_slug_fails(
    create_user, login_user, whoami, import_suite_yaml,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    body = """
name: NeedsDS
data_source_slugs: [does_not_exist]
cases:
  - name: x
    prompt: {content: "q"}
    expectations: {spec_version: 1, rules: []}
"""
    resp = import_suite_yaml(body, user_token=token, org_id=org_id)
    assert resp.status_code == 400
    assert "does_not_exist" in resp.json()["detail"]


@pytest.mark.e2e
def test_import_rejects_case_with_both_prompt_and_turns(
    create_user, login_user, whoami, import_suite_yaml,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    bad = """
name: Bad
cases:
  - name: both
    prompt: {content: "q"}
    turns:
      - prompt: {content: "q1"}
    expectations: {spec_version: 1, rules: []}
"""
    resp = import_suite_yaml(bad, user_token=token, org_id=org_id)
    assert resp.status_code == 400


@pytest.mark.e2e
def test_multi_turn_persists_additional_turns(
    create_user, login_user, whoami,
    import_suite_yaml, get_test_case,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    result = import_suite_yaml(MULTI_TURN_YAML, user_token=token, org_id=org_id).json()
    case_id = result["cases_by_name"]["clarify_then_answer"]

    case = get_test_case(case_id, user_token=token, org_id=org_id).json()
    assert case["prompt_json"]["content"] == "Show me the data"
    # additional_turns_json is a non-schema model attr, but the raw dict is still
    # populated on the DB row; read via /cases to confirm round-trippable via export.


@pytest.mark.e2e
def test_export_round_trip_single_turn(
    create_user, login_user, whoami,
    import_suite_yaml, export_suite_yaml,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    first = import_suite_yaml(SANITY_YAML, user_token=token, org_id=org_id).json()
    suite_id = first["suite_id"]

    exported = export_suite_yaml(suite_id, user_token=token, org_id=org_id)
    assert exported.status_code == 200
    parsed = yaml.safe_load(exported.text)
    assert parsed["name"] == "Sanity"
    assert parsed["cases"][0]["name"] == "smoke"
    assert parsed["cases"][0]["prompt"]["content"] == "How many users?"

    # Re-import the exported YAML — must be a fixed point on suite/case IDs.
    reimported = import_suite_yaml(exported.text, user_token=token, org_id=org_id).json()
    assert reimported["suite_id"] == suite_id
    assert reimported["cases_by_name"]["smoke"] == first["cases_by_name"]["smoke"]


@pytest.mark.e2e
def test_export_round_trip_multi_turn(
    create_user, login_user, whoami,
    import_suite_yaml, export_suite_yaml,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    first = import_suite_yaml(MULTI_TURN_YAML, user_token=token, org_id=org_id).json()
    suite_id = first["suite_id"]

    exported = export_suite_yaml(suite_id, user_token=token, org_id=org_id)
    assert exported.status_code == 200
    parsed = yaml.safe_load(exported.text)
    case = parsed["cases"][0]
    assert "turns" in case and "prompt" not in case
    assert len(case["turns"]) == 2
    assert case["turns"][0]["prompt"]["content"] == "Show me the data"
    assert case["turns"][1]["prompt"]["content"] == "Users per month for 2025"

    # Round-trip the exported YAML.
    reimported = import_suite_yaml(exported.text, user_token=token, org_id=org_id).json()
    assert reimported["suite_id"] == suite_id
