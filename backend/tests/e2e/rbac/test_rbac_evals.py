"""
RBAC end-to-end coverage for /api/tests (eval suites + cases).

The route layer mixes ``@requires_permission('manage_evals')`` (org-level
gate, not resource_scoped) on suite endpoints with
``@requires_permission('manage_evals', resource_scoped=True)`` on case
endpoints. The case mutating routes also call
``check_resource_permissions(..., 'manage_evals')`` against any
``data_source_ids_json`` provided in the request body, so per-DS evals
authors can scope cases to their DS only.

(Note: prior to the fix shipped in this branch the imperative call
referenced ``"create_evals"`` which is not a registered resource
permission, so per-DS evals authors were locked out entirely. See the
PR description for context.)
"""
import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def evals_world(
    test_client,
    bootstrap_admin,
    invite_user_to_org,
    sqlite_data_source,
    grant_resource,
):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    ds_a = sqlite_data_source(name="ev_ds_a", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name="ev_ds_b", user_token=admin["token"], org_id=org_id)

    for ds in (ds_a, ds_b):
        test_client.put(
            f"/api/data_sources/{ds['id']}",
            json={"is_public": False},
            headers=_hdr(admin["token"], org_id),
        )

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_a_evaluator = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grant_resource(
        resource_type="data_source",
        resource_id=ds_a["id"],
        principal_type="user",
        principal_id=ds_a_evaluator["user_id"],
        permissions=["view", "view_schema", "manage_evals"],
        user_token=admin["token"],
        org_id=org_id,
    )

    return {
        "org_id": org_id,
        "ds_a": ds_a,
        "ds_b": ds_b,
        "principals": {
            "admin": admin,
            "member": member,
            "ds_a_evaluator": ds_a_evaluator,
        },
    }


# ────────────────────────────────────────────────────────────────────
# Suite endpoints — strictly org-level
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_eval_suite_endpoints_require_org_manage_evals(test_client, evals_world):
    """POST/GET /tests/suites are gated by org-level manage_evals only.

    Per-DS evaluators (whose only manage_evals permission is at the
    resource level) cannot list or create suites — that requires the
    org-level perm. Members likewise cannot.
    """
    org_id = evals_world["org_id"]

    admin = evals_world["principals"]["admin"]
    evaluator = evals_world["principals"]["ds_a_evaluator"]
    member = evals_world["principals"]["member"]

    # Admin can create a suite
    suite = test_client.post(
        "/api/tests/suites",
        json={"name": "Admin Suite", "description": None},
        headers=_hdr(admin["token"], org_id),
    )
    assert suite.status_code == 200, suite.text
    suite_id = suite.json()["id"]

    # Admin can list
    listing = test_client.get(
        "/api/tests/suites",
        headers=_hdr(admin["token"], org_id),
    )
    assert listing.status_code == 200, listing.text

    # Member cannot create
    bad = test_client.post(
        "/api/tests/suites",
        json={"name": "x", "description": None},
        headers=_hdr(member["token"], org_id),
    )
    assert bad.status_code == 403, bad.text

    # Per-DS evaluator cannot create suites either (resource_scoped is
    # not set on the suite endpoints — only the org-level gate applies)
    eval_bad = test_client.post(
        "/api/tests/suites",
        json={"name": "y", "description": None},
        headers=_hdr(evaluator["token"], org_id),
    )
    assert eval_bad.status_code == 403, eval_bad.text


# ────────────────────────────────────────────────────────────────────
# Case endpoint — resource_scoped, with check_resource_permissions
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_eval_case_create_resource_scoped(test_client, evals_world):
    """Cover the per-DS gate on POST /tests/suites/{id}/cases.

    The case route is ``resource_scoped=True``, so the org-level
    decorator lets a non-admin through, then ``check_resource_permissions``
    enforces ``manage_evals`` on each DS in ``data_source_ids_json``.

    Expected:
      admin            → can create cases on ds_a, ds_b, and global
      ds_a_evaluator   → can create cases on ds_a, denied on ds_b
      member           → denied on every DS-scoped case (no grant)
    """
    org_id = evals_world["org_id"]
    admin = evals_world["principals"]["admin"]
    evaluator = evals_world["principals"]["ds_a_evaluator"]
    member = evals_world["principals"]["member"]

    # Admin creates a suite (cases need a parent suite)
    suite = test_client.post(
        "/api/tests/suites",
        json={"name": "Eval RBAC Suite", "description": None},
        headers=_hdr(admin["token"], org_id),
    )
    assert suite.status_code == 200, suite.text
    suite_id = suite.json()["id"]

    def _make_case(token, ds_ids):
        return test_client.post(
            f"/api/tests/suites/{suite_id}/cases",
            json={
                "name": f"case-{ds_ids}",
                "prompt_json": {"content": "evaluate"},
                "expectations_json": {"spec_version": 1, "rules": [], "order_mode": "flexible"},
                "data_source_ids_json": ds_ids,
            },
            headers=_hdr(token, org_id),
        )

    ds_a_id = evals_world["ds_a"]["id"]
    ds_b_id = evals_world["ds_b"]["id"]

    failures = []
    for principal_name, ds_ids, want in [
        ("admin", [ds_a_id], 200),
        ("admin", [ds_b_id], 200),
        ("admin", [], 200),
        ("ds_a_evaluator", [ds_a_id], 200),
        ("ds_a_evaluator", [ds_b_id], 403),
        ("member", [ds_a_id], 403),
        ("member", [ds_b_id], 403),
    ]:
        token = evals_world["principals"][principal_name]["token"]
        resp = _make_case(token, ds_ids)
        if resp.status_code != want:
            failures.append(
                f"{principal_name} ds={ds_ids}: want {want} got {resp.status_code} ({resp.text[:160]})"
            )

    assert not failures, "\n".join(failures)
