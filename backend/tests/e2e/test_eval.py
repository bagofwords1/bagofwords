import pytest


@pytest.mark.e2e
def test_create_and_get_suite(create_user, login_user, whoami, get_test_suites, create_test_suite, get_test_suite):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    initial = get_test_suites(user_token=token, org_id=org_id)
    new_suite = create_test_suite(name="E2E Suite", description="End-to-end suite", user_token=token, org_id=org_id)
    assert new_suite["name"] == "E2E Suite"

    after = get_test_suites(user_token=token, org_id=org_id)
    assert isinstance(after, list)
    assert len(after) == len(initial) + 1

    resp = get_test_suite(new_suite["id"], user_token=token, org_id=org_id)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == new_suite["id"]
    assert data["name"] == "E2E Suite"


@pytest.mark.e2e
def test_get_suite_404(create_user, login_user, whoami, get_test_suite):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = get_test_suite("non-existent-suite-id", user_token=token, org_id=org_id)
    assert resp.status_code == 404
    assert "Test suite not found" in resp.json()["detail"]


@pytest.mark.e2e
def test_create_case_and_get(create_user, login_user, whoami, create_test_suite, get_test_cases, create_test_case, get_test_case):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    suite = create_test_suite(name="Cases Suite", user_token=token, org_id=org_id)
    cases_before = get_test_cases(suite["id"], user_token=token, org_id=org_id)
    assert isinstance(cases_before, list)

    case = create_test_case(
        suite_id=suite["id"],
        name="Case 1",
        user_token=token,
        org_id=org_id,
    )
    assert case["name"] == "Case 1"
    assert "prompt_json" in case
    assert case["prompt_json"].get("content") == "Evaluate this output."

    cases_after = get_test_cases(suite["id"], user_token=token, org_id=org_id)
    assert len(cases_after) == len(cases_before) + 1

    resp = get_test_case(case["id"], user_token=token, org_id=org_id)
    assert resp.status_code == 200
    got = resp.json()
    assert got["id"] == case["id"]
    assert got["name"] == "Case 1"
    assert got["suite_id"] == suite["id"]
    assert got["prompt_json"]["content"] == "Evaluate this output."


@pytest.mark.e2e
def test_get_case_404(create_user, login_user, whoami, get_test_case):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    resp = get_test_case("non-existent-case-id", user_token=token, org_id=org_id)
    assert resp.status_code == 404
    assert "Test case not found" in resp.json()["detail"]


