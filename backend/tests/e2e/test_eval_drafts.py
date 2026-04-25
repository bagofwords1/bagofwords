"""E2E tests for the eval-as-tools lifecycle pieces that don't need an LLM.

The drafter agent itself (knowledge harness + create_eval tool) is
exercised separately. Here we cover the durable bits:

- ``status`` field round-trips through the API (default ``active``).
- ``PATCH /tests/cases/{id}/status`` promotes drafts and validates input.
- A draft case is excluded from suite-level runs but is runnable when
  selected explicitly via ``case_ids``.
"""
import pytest

from app.models.eval import TEST_CASE_STATUS_ACTIVE, TEST_CASE_STATUS_DRAFT


def _patch_status(test_client, case_id, status, *, user_token, org_id):
    return test_client.patch(
        f"/api/tests/cases/{case_id}/status",
        json={"status": status},
        headers={
            "Authorization": f"Bearer {user_token}",
            "X-Organization-Id": str(org_id),
        },
    )


@pytest.mark.e2e
def test_test_case_default_status_is_active(
    create_user, login_user, whoami,
    create_test_suite, create_test_case,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    suite = create_test_suite(name="Status Defaults", user_token=token, org_id=org_id)
    case = create_test_case(suite_id=suite["id"], user_token=token, org_id=org_id)

    assert case.get("status") == TEST_CASE_STATUS_ACTIVE
    assert case.get("auto_generated") is False


@pytest.mark.e2e
def test_patch_case_status_round_trip(
    create_user, login_user, whoami,
    create_test_suite, create_test_case,
    test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    suite = create_test_suite(name="Promote Suite", user_token=token, org_id=org_id)
    case = create_test_case(suite_id=suite["id"], user_token=token, org_id=org_id)

    # Demote to draft, then promote back to active.
    resp = _patch_status(test_client, case["id"], TEST_CASE_STATUS_DRAFT, user_token=token, org_id=org_id)
    assert resp.status_code == 200, resp.json()
    assert resp.json()["status"] == TEST_CASE_STATUS_DRAFT

    resp = _patch_status(test_client, case["id"], TEST_CASE_STATUS_ACTIVE, user_token=token, org_id=org_id)
    assert resp.status_code == 200, resp.json()
    assert resp.json()["status"] == TEST_CASE_STATUS_ACTIVE


@pytest.mark.e2e
def test_patch_case_status_rejects_unknown_value(
    create_user, login_user, whoami,
    create_test_suite, create_test_case,
    test_client,
):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    suite = create_test_suite(name="Bad Status", user_token=token, org_id=org_id)
    case = create_test_case(suite_id=suite["id"], user_token=token, org_id=org_id)

    resp = _patch_status(test_client, case["id"], "not-a-status", user_token=token, org_id=org_id)
    assert resp.status_code == 400


@pytest.mark.e2e
def test_run_suite_skips_draft_cases(
    create_user, login_user, whoami,
    create_test_suite, create_test_case,
    test_client,
):
    """``_get_cases`` defaults to ``status='active'``, so a draft case in
    the same suite must not produce a TestResult on a suite-level run."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    suite = create_test_suite(name="Filter Suite", user_token=token, org_id=org_id)
    active_case = create_test_case(suite_id=suite["id"], name="Active", user_token=token, org_id=org_id)
    draft_case = create_test_case(suite_id=suite["id"], name="Draft", user_token=token, org_id=org_id)

    resp = _patch_status(test_client, draft_case["id"], TEST_CASE_STATUS_DRAFT, user_token=token, org_id=org_id)
    assert resp.status_code == 200

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    resp = test_client.post(
        f"/api/tests/suites/{suite['id']}/runs",
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    run_id = resp.json()["id"]

    results_resp = test_client.get(
        f"/api/tests/runs/{run_id}/results",
        headers=headers,
    )
    assert results_resp.status_code == 200, results_resp.json()
    case_ids = {r["case_id"] for r in results_resp.json()}
    assert active_case["id"] in case_ids
    assert draft_case["id"] not in case_ids


@pytest.mark.e2e
def test_explicit_case_ids_can_run_drafts(
    create_user, login_user, whoami,
    create_test_suite, create_test_case,
    test_client,
):
    """Drafts must remain runnable on-demand when explicitly named."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    suite = create_test_suite(name="Explicit Suite", user_token=token, org_id=org_id)
    draft_case = create_test_case(suite_id=suite["id"], name="Draft Run", user_token=token, org_id=org_id)

    resp = _patch_status(test_client, draft_case["id"], TEST_CASE_STATUS_DRAFT, user_token=token, org_id=org_id)
    assert resp.status_code == 200

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }
    resp = test_client.post(
        "/api/tests/runs",
        json={"case_ids": [draft_case["id"]], "trigger_reason": "manual"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.json()
    run_id = resp.json()["id"]

    results_resp = test_client.get(
        f"/api/tests/runs/{run_id}/results",
        headers=headers,
    )
    assert results_resp.status_code == 200, results_resp.json()
    case_ids = {r["case_id"] for r in results_resp.json()}
    assert draft_case["id"] in case_ids
