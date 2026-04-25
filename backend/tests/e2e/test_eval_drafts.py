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
@pytest.mark.asyncio
async def test_get_cases_default_filter_skips_drafts(
    create_user, login_user, whoami,
    create_test_suite, create_test_case,
    test_client,
):
    """``TestRunService._get_cases`` defaults to ``status='active'`` so
    suite-level / scheduled runs skip drafts. We test the filter directly
    rather than through ``POST /tests/runs`` because the HTTP path
    requires a configured default LLM model — orthogonal to filtering."""
    from app.dependencies import async_session_maker
    from app.services.test_run_service import TestRunService

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    suite = create_test_suite(name="Filter Suite", user_token=token, org_id=org_id)
    active_case = create_test_case(suite_id=suite["id"], name="Active", user_token=token, org_id=org_id)
    draft_case = create_test_case(suite_id=suite["id"], name="Draft", user_token=token, org_id=org_id)

    resp = _patch_status(test_client, draft_case["id"], TEST_CASE_STATUS_DRAFT, user_token=token, org_id=org_id)
    assert resp.status_code == 200

    service = TestRunService()
    async with async_session_maker() as db:
        active_only = await service._get_cases(db, suite["id"])
        all_cases = await service._get_cases(db, suite["id"], status=None)

    active_ids = {str(c.id) for c in active_only}
    all_ids = {str(c.id) for c in all_cases}

    assert active_case["id"] in active_ids
    assert draft_case["id"] not in active_ids
    assert {active_case["id"], draft_case["id"]} <= all_ids


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_resolve_cases_inputs_includes_drafts_when_explicit(
    create_user, login_user, whoami,
    create_test_suite, create_test_case,
    test_client,
):
    """Explicit ``case_ids`` must bypass the active-only default — drafts
    remain runnable on demand. Tested at the service level for the same
    reason as above (HTTP path needs an LLM model)."""
    from app.dependencies import async_session_maker
    from app.services.test_run_service import TestRunService

    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]

    suite = create_test_suite(name="Explicit Suite", user_token=token, org_id=org_id)
    draft_case = create_test_case(suite_id=suite["id"], name="Draft Run", user_token=token, org_id=org_id)

    resp = _patch_status(test_client, draft_case["id"], TEST_CASE_STATUS_DRAFT, user_token=token, org_id=org_id)
    assert resp.status_code == 200

    service = TestRunService()
    async with async_session_maker() as db:
        resolved = await service._resolve_cases_inputs(
            db,
            organization_id=str(org_id),
            case_ids=[draft_case["id"]],
            suite_id=None,
        )

    resolved_ids = {str(c.id) for c in resolved}
    assert draft_case["id"] in resolved_ids
