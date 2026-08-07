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
import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.eval import TestResult, TestRun



def _seed_run(case_id):
    """Insert a TestRun with one TestResult for ``case_id`` and return its id."""
    async def _go():
        async with async_session_maker() as db:
            run = TestRun(
                title=f"seeded-{case_id[:8]}",
                suite_ids="",
                status="success",
                trigger_reason="manual",
            )
            db.add(run)
            await db.flush()
            db.add(TestResult(
                run_id=str(run.id),
                case_id=str(case_id),
                head_completion_id=str(uuid.uuid4()),
                status="pass",
            ))
            await db.commit()
            return str(run.id)

    return asyncio.run(_go())


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

    # sqlite_data_source defaults to is_public=False and asserts the flip.
    ds_a = sqlite_data_source(name="ev_ds_a", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name="ev_ds_b", user_token=admin["token"], org_id=org_id)

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    ds_a_evaluator = invite_user_to_org(org_id=org_id, admin_token=admin["token"])

    grant_resource(
        resource_type="data_source",
        resource_id=ds_a["id"],
        principal_type="user",
        principal_id=ds_a_evaluator["user_id"],
        permissions=["manage_evals"],
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
def test_eval_suite_endpoints_allow_per_agent_evaluators(test_client, evals_world):
    """POST/GET /tests/suites accept a per-agent ``manage_evals`` grant.

    A suite is a container, not an agent-scoped object: the authority that
    matters is enforced on the CASES inside it (each case is gated on the
    agents in its ``data_source_ids_json``). Requiring the org-level perm to
    create one left per-agent evaluators unable to put a case anywhere — the
    agent's Evals panel rendered as a permanently empty list of 403s.

    They create it HOMED on an agent they manage. A suite with no home is an
    org-wide shelf holding cases that run against every agent, so opening one is
    org-level — the same bar as authoring an agent-less case.

    Principals with NO eval authority anywhere (plain members) are still
    refused: the resource_scoped decorator requires the permission on at
    least one agent before the route body runs.
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

    # Member holds manage_evals on nothing → refused at the door.
    bad = test_client.post(
        "/api/tests/suites",
        json={"name": "x", "description": None},
        headers=_hdr(member["token"], org_id),
    )
    assert bad.status_code == 403, bad.text
    bad_list = test_client.get(
        "/api/tests/suites", headers=_hdr(member["token"], org_id),
    )
    assert bad_list.status_code == 403, bad_list.text

    # Per-agent evaluator CAN create and list suites — they need somewhere to
    # author the cases their grant entitles them to write — on THEIR agent.
    eval_ok = test_client.post(
        "/api/tests/suites",
        json={"name": "y", "description": None, "data_source_id": evals_world["ds_a"]["id"]},
        headers=_hdr(evaluator["token"], org_id),
    )
    assert eval_ok.status_code == 200, eval_ok.text
    # ...but not an org-wide shelf.
    assert test_client.post(
        "/api/tests/suites",
        json={"name": "y-global", "description": None},
        headers=_hdr(evaluator["token"], org_id),
    ).status_code == 403
    eval_list = test_client.get(
        "/api/tests/suites", headers=_hdr(evaluator["token"], org_id),
    )
    assert eval_list.status_code == 200, eval_list.text


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


# ────────────────────────────────────────────────────────────────────
# Agent narrowing — presentation, on top of authority
# ────────────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_case_and_run_listings_narrow_to_one_agent(test_client, evals_world):
    """``data_source_id`` / ``scope=global`` narrow the case and run listings.

    Both listings used to be org-wide only, and the per-agent Evals panel
    narrowed a PAGE of them in the browser: the newest N runs in the
    organization, kept if they touched one of the newest M cases. An agent whose
    runs sat past the org's newest N therefore showed "no runs" while having
    plenty, and deleting a test case erased every past run of it from the
    agent's history — the runs were only reachable *via* the case list.

    Narrowing server-side is what makes the page per-agent. This asserts the
    semantics that narrowing has to have:

      - an agent's listing includes the agent-less cases, which run against
        every agent and so are part of what it is tested by;
      - ``scope=global`` shows only those agent-less ones;
      - another agent's cases never appear;
      - and a run survives its case being deleted.
    """
    org_id = evals_world["org_id"]
    admin = evals_world["principals"]["admin"]
    hdr = _hdr(admin["token"], org_id)
    ds_a_id = evals_world["ds_a"]["id"]
    ds_b_id = evals_world["ds_b"]["id"]

    suite_id = test_client.post(
        "/api/tests/suites",
        json={"name": "Narrowing Suite", "description": None},
        headers=hdr,
    ).json()["id"]

    def _case(name, ds_ids):
        resp = test_client.post(
            f"/api/tests/suites/{suite_id}/cases",
            json={
                "name": name,
                "prompt_json": {"content": name},
                "expectations_json": {"spec_version": 1, "rules": [], "order_mode": "flexible"},
                "data_source_ids_json": ds_ids,
            },
            headers=hdr,
        )
        assert resp.status_code == 200, resp.text
        return resp.json()["id"]

    case_a = _case("targets-a", [ds_a_id])
    case_b = _case("targets-b", [ds_b_id])
    case_global = _case("targets-nobody", [])

    def _case_ids(query):
        resp = test_client.get(f"/api/tests/cases?limit=1000&{query}", headers=hdr)
        assert resp.status_code == 200, resp.text
        return {c["id"] for c in resp.json()}

    for_a = _case_ids(f"data_source_id={ds_a_id}")
    assert case_a in for_a and case_global in for_a
    assert case_b not in for_a

    for_global = _case_ids("scope=global")
    assert case_global in for_global
    assert case_a not in for_global and case_b not in for_global

    # No narrowing params → unchanged behaviour, everything the caller may read.
    unnarrowed = _case_ids("")
    assert {case_a, case_b, case_global} <= unnarrowed

    # Runs follow the cases they executed. Seeded directly rather than through
    # POST /tests/runs: that route needs a configured default model and starts
    # real agent work, none of which this assertion is about.
    run_a = _seed_run(case_a)
    run_b = _seed_run(case_b)

    def _run_ids(query):
        resp = test_client.get(f"/api/tests/runs?limit=100&{query}", headers=hdr)
        assert resp.status_code == 200, resp.text
        return {r["id"] for r in resp.json()}

    runs_for_a = _run_ids(f"data_source_id={ds_a_id}")
    assert run_a in runs_for_a
    assert run_b not in runs_for_a

    # The regression the narrowing exists to prevent: deleting the case must not
    # take its run history with it.
    assert test_client.delete(f"/api/tests/cases/{case_a}", headers=hdr).status_code == 200
    assert case_a not in _case_ids(f"data_source_id={ds_a_id}")
    assert run_a in _run_ids(f"data_source_id={ds_a_id}")
