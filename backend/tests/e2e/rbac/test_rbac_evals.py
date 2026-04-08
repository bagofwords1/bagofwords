"""
Eval (test suites/cases/runs) RBAC tests.

In this branch every /api/tests/* endpoint is gated by ``manage_tests``
which is admin-only — there is no member-level eval permission. The
matrix is therefore simple: admin can do everything, member is 403 on
every operation, outsider is 403/404.

These tests use the rbac_principals cast to verify the gate. We do NOT
exercise the actual run scheduling because that requires LLM config.
"""
import pytest


def _h(token, org_id):
    return {
        "Authorization": f"Bearer {token}",
        "X-Organization-Id": str(org_id),
    }


@pytest.mark.e2e
def test_admin_can_create_suite_member_cannot(test_client, rbac_principals):
    """Admin: 200 on suite/case CRUD; member: 403 on every endpoint."""
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]
    member = rbac_principals["principals"]["member"]

    # Admin creates a suite + case
    suite_resp = test_client.post(
        "/api/tests/suites",
        json={"name": "RBAC Suite", "description": "rbac"},
        headers=_h(admin["token"], org_id),
    )
    assert suite_resp.status_code == 200, suite_resp.json()
    suite_id = suite_resp.json()["id"]

    case_resp = test_client.post(
        f"/api/tests/suites/{suite_id}/cases",
        json={
            "name": "RBAC Case",
            "prompt_json": {"content": "do a thing"},
            "expectations_json": {
                "spec_version": 1,
                "rules": [],
                "order_mode": "flexible",
            },
            "data_source_ids_json": [],
        },
        headers=_h(admin["token"], org_id),
    )
    assert case_resp.status_code == 200, case_resp.json()
    case_id = case_resp.json()["id"]

    # Member: every read AND write returns 403 because manage_tests is admin only.
    # Use schema-valid payloads so request validation passes and the permission
    # decorator actually runs (otherwise FastAPI would 422 first).
    valid_suite_body = {"name": "RBAC member-attempt", "description": "no-op"}
    valid_case_body = {
        "name": "member case",
        "prompt_json": {"content": "x"},
        "expectations_json": {
            "spec_version": 1,
            "rules": [],
            "order_mode": "flexible",
        },
        "data_source_ids_json": [],
    }
    valid_run_body = {"case_ids": [case_id], "trigger_reason": "manual"}

    member_endpoints = [
        ("GET",  "/api/tests/suites",                           None),
        ("GET",  f"/api/tests/suites/{suite_id}",               None),
        ("POST", "/api/tests/suites",                           valid_suite_body),
        ("GET",  f"/api/tests/suites/{suite_id}/cases",         None),
        ("GET",  f"/api/tests/cases/{case_id}",                 None),
        ("POST", f"/api/tests/suites/{suite_id}/cases",         valid_case_body),
        ("GET",  "/api/tests/runs",                             None),
        ("POST", "/api/tests/runs",                             valid_run_body),
        ("GET",  "/api/tests/metrics",                          None),
    ]
    member_failures = []
    for method, url, body in member_endpoints:
        if method == "GET":
            r = test_client.get(url, headers=_h(member["token"], org_id))
        else:
            r = test_client.request(
                method, url, json=body, headers=_h(member["token"], org_id)
            )
        if r.status_code != 403:
            member_failures.append(
                f"{method} {url}: expected 403, got {r.status_code}"
            )
    assert not member_failures, (
        "manage_tests gate failures for member:\n" + "\n".join(member_failures)
    )


@pytest.mark.e2e
def test_admin_can_view_runs_and_metrics(test_client, rbac_principals):
    """Admin can hit the read endpoints — these were occasional regression sources."""
    org_id = rbac_principals["org_id"]
    admin = rbac_principals["principals"]["admin"]

    suites = test_client.get(
        "/api/tests/suites", headers=_h(admin["token"], org_id)
    )
    assert suites.status_code == 200, suites.json()

    summary = test_client.get(
        "/api/tests/suites/summary", headers=_h(admin["token"], org_id)
    )
    assert summary.status_code == 200, summary.json()

    runs = test_client.get(
        "/api/tests/runs", headers=_h(admin["token"], org_id)
    )
    assert runs.status_code == 200, runs.json()


@pytest.mark.e2e
def test_outsider_cannot_access_eval_endpoints(test_client, rbac_principals):
    """Outsider gets 403/404 from every /api/tests/* endpoint in this org."""
    org_id = rbac_principals["org_id"]
    outsider = rbac_principals["principals"]["outsider"]
    headers = _h(outsider["token"], org_id)

    for method, url, body in [
        ("GET",  "/api/tests/suites", None),
        ("POST", "/api/tests/suites", {"name": "evil suite"}),
        ("GET",  "/api/tests/runs",   None),
    ]:
        if method == "GET":
            r = test_client.get(url, headers=headers)
        else:
            r = test_client.request(method, url, json=body, headers=headers)
        assert r.status_code in (403, 404), (
            f"outsider {method} {url}: expected 403/404, got {r.status_code}"
        )
