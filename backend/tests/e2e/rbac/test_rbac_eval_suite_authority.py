"""Eval suites, runs and results follow the INSTRUCTION access model.

The rules, mirrored from ``routes/instruction.py`` / ``user_can_view_instruction``:

  - **Read is a union** over the agents a case targets — authority over any one
    of them lets you see the row. A routing eval spanning agents A and B governs
    both, so A's manager must see that it exists.
  - **Write is an intersection** — mutating a case needs authority over EVERY
    agent it targets, so A's manager cannot change what it asserts about B.
  - **Agent-less (global) cases are visible to everyone, editable org-level
    only** — exactly like a global instruction, which reaches every agent.
  - **A suite is a folder**, not an agent-owned container. Its authority derives
    from the cases it holds: adding is cheap, destroying is an intersection.

The leak this pins: runs, results and especially ``/results/{id}/transcript``
were scoped by ORGANIZATION only. ``resource_scoped=True`` on the decorator is
an admission test (manage_evals org-wide OR on any one agent) and nothing past
it narrowed by agent, so a grant on a single agent read every run in the org —
including transcripts, which carry the run's actual query output.
"""
import uuid

import pytest


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


@pytest.fixture
def two_agent_world(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource
):
    """Agents A and B with a manager each, plus an org-level eval admin."""
    admin = bootstrap_admin("evadmin")
    org_id = admin["org_id"]
    ds_a = sqlite_data_source(name=f"ag_a_{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    ds_b = sqlite_data_source(name=f"ag_b_{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)

    mgr_a = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    mgr_b = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    for user, ds in ((mgr_a, ds_a), (mgr_b, ds_b)):
        grant_resource(
            resource_type="data_source", resource_id=ds["id"],
            principal_type="user", principal_id=user["user_id"],
            permissions=["manage_evals"], user_token=admin["token"], org_id=org_id,
        )

    suite = test_client.post(
        "/api/tests/suites", json={"name": f"s_{uuid.uuid4().hex[:6]}"},
        headers=_hdr(admin["token"], org_id),
    )
    assert suite.status_code == 200, suite.text
    return {
        "org_id": org_id, "admin": admin, "ds_a": ds_a, "ds_b": ds_b,
        "mgr_a": mgr_a, "mgr_b": mgr_b, "suite_id": suite.json()["id"],
    }


def _case(test_client, token, org_id, suite_id, ds_ids, name=None):
    resp = test_client.post(
        f"/api/tests/suites/{suite_id}/cases",
        json={
            "name": name or f"c_{uuid.uuid4().hex[:6]}",
            "prompt_json": {"content": "how many rows?"},
            "expectations_json": {"rules": []},
            "data_source_ids_json": ds_ids,
        },
        headers=_hdr(token, org_id),
    )
    return resp


# ── read is a union, write is an intersection ────────────────────────


@pytest.mark.e2e
def test_routing_eval_is_visible_to_each_agent_manager_but_editable_by_neither(
    test_client, two_agent_world
):
    """A case spanning A and B: both managers SEE it (union), neither may EDIT
    it (intersection). This is the case that motivated the model — an eval that
    verifies routing between two agents legitimately belongs to both."""
    w = two_agent_world
    created = _case(
        test_client, w["admin"]["token"], w["org_id"], w["suite_id"],
        [w["ds_a"]["id"], w["ds_b"]["id"]],
    )
    assert created.status_code == 200, created.text
    case_id = created.json()["id"]

    for mgr in (w["mgr_a"], w["mgr_b"]):
        listed = test_client.get(
            "/api/tests/cases?limit=500", headers=_hdr(mgr["token"], w["org_id"])
        )
        assert listed.status_code == 200, listed.text
        assert case_id in {c["id"] for c in listed.json()}, \
            "a routing eval governs this manager's agent — they must see it"

        edited = test_client.patch(
            f"/api/tests/cases/{case_id}",
            json={"name": "hijacked"}, headers=_hdr(mgr["token"], w["org_id"]),
        )
        assert edited.status_code == 403, \
            "editing a case that also targets an agent you don't manage must 403"


@pytest.mark.e2e
def test_single_agent_case_is_hidden_from_the_other_manager(
    test_client, two_agent_world
):
    """The union must not leak: a case targeting only A is invisible to B's
    manager."""
    w = two_agent_world
    created = _case(
        test_client, w["admin"]["token"], w["org_id"], w["suite_id"], [w["ds_a"]["id"]]
    )
    assert created.status_code == 200, created.text
    case_id = created.json()["id"]

    seen_b = test_client.get(
        "/api/tests/cases?limit=500", headers=_hdr(w["mgr_b"]["token"], w["org_id"])
    )
    assert case_id not in {c["id"] for c in seen_b.json()}

    seen_a = test_client.get(
        "/api/tests/cases?limit=500", headers=_hdr(w["mgr_a"]["token"], w["org_id"])
    )
    assert case_id in {c["id"] for c in seen_a.json()}


@pytest.mark.e2e
def test_global_case_is_visible_to_all_but_editable_only_org_level(
    test_client, two_agent_world
):
    """Like a global instruction: an agent-less case reaches every agent, so
    everyone sees it and only an org-level admin may change it."""
    w = two_agent_world
    created = _case(test_client, w["admin"]["token"], w["org_id"], w["suite_id"], [])
    assert created.status_code == 200, created.text
    case_id = created.json()["id"]

    listed = test_client.get(
        "/api/tests/cases?limit=500", headers=_hdr(w["mgr_a"]["token"], w["org_id"])
    )
    assert case_id in {c["id"] for c in listed.json()}, \
        "a global eval runs against this manager's agent — it must be visible"

    assert test_client.patch(
        f"/api/tests/cases/{case_id}", json={"name": "nope"},
        headers=_hdr(w["mgr_a"]["token"], w["org_id"]),
    ).status_code == 403

    # And a per-agent manager cannot author one either.
    assert _case(
        test_client, w["mgr_a"]["token"], w["org_id"], w["suite_id"], []
    ).status_code == 403


# ── suites are folders whose authority derives from their cases ──────


@pytest.mark.e2e
def test_agent_manager_can_organize_and_delete_their_own_suite(
    test_client, two_agent_world
):
    """The reported workflow: create a suite, file your own cases into it, move
    one out, delete it — all holding a grant on a single agent."""
    w = two_agent_world
    tok, org = w["mgr_a"]["token"], w["org_id"]

    mine = test_client.post(
        "/api/tests/suites", json={"name": f"mine_{uuid.uuid4().hex[:6]}"},
        headers=_hdr(tok, org),
    )
    assert mine.status_code == 200, mine.text
    mine_id = mine.json()["id"]

    ids = [
        _case(test_client, tok, org, mine_id, [w["ds_a"]["id"]]).json()["id"]
        for _ in range(3)
    ]

    other = test_client.post(
        "/api/tests/suites", json={"name": f"other_{uuid.uuid4().hex[:6]}"},
        headers=_hdr(tok, org),
    )
    other_id = other.json()["id"]
    moved = test_client.patch(
        f"/api/tests/cases/{ids[0]}", json={"suite_id": other_id},
        headers=_hdr(tok, org),
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["suite_id"] == other_id, "a case must be movable between suites"

    assert test_client.patch(
        f"/api/tests/suites/{mine_id}", json={"name": "renamed"},
        headers=_hdr(tok, org),
    ).status_code == 200
    assert test_client.delete(
        f"/api/tests/suites/{mine_id}", headers=_hdr(tok, org)
    ).status_code == 200


@pytest.mark.e2e
def test_deleting_a_suite_reparents_cases_you_may_not_destroy(
    test_client, two_agent_world
):
    """Dropping a case into someone else's suite must not lock them out of it.
    B's case is reparented to Drafts and survives; A's manager still deletes the
    suite, and destroys only their own case."""
    w = two_agent_world
    org = w["org_id"]

    shared = test_client.post(
        "/api/tests/suites", json={"name": f"shared_{uuid.uuid4().hex[:6]}"},
        headers=_hdr(w["mgr_a"]["token"], org),
    ).json()["id"]

    a_case = _case(test_client, w["mgr_a"]["token"], org, shared, [w["ds_a"]["id"]]).json()["id"]
    b_case = _case(test_client, w["mgr_b"]["token"], org, shared, [w["ds_b"]["id"]]).json()["id"]

    deleted = test_client.delete(
        f"/api/tests/suites/{shared}", headers=_hdr(w["mgr_a"]["token"], org)
    )
    assert deleted.status_code == 200, deleted.text

    # B's case survived, in a different suite; A's case went with the suite.
    still_there = test_client.get(
        f"/api/tests/cases/{b_case}", headers=_hdr(w["mgr_b"]["token"], org)
    )
    assert still_there.status_code == 200, "a foreign case must be reparented, not destroyed"
    assert still_there.json()["suite_id"] != shared

    assert test_client.get(
        f"/api/tests/cases/{a_case}", headers=_hdr(w["mgr_a"]["token"], org)
    ).status_code == 404


@pytest.mark.e2e
def test_per_agent_evaluator_can_author_a_complete_case(test_client, two_agent_world):
    """Authoring is not just POSTing the case — the modal loads the expectation
    catalogs to render the "Add rule" UI. Those were gated org-only while case
    creation was resource-scoped, so a per-agent evaluator could create a case
    and then hit 403 the moment they tried to add an expectation to it."""
    w = two_agent_world
    hdr = _hdr(w["mgr_a"]["token"], w["org_id"])

    assert test_client.get("/api/tests/catalog", headers=hdr).status_code == 200
    assert test_client.get("/api/tests/rules/catalog", headers=hdr).status_code == 200

    created = _case(
        test_client, w["mgr_a"]["token"], w["org_id"], w["suite_id"], [w["ds_a"]["id"]]
    )
    assert created.status_code == 200, created.text


@pytest.mark.e2e
def test_per_agent_evaluator_can_watch_the_run_they_started(
    test_client, two_agent_world
):
    """Starting a run and being unable to observe it is not a coherent
    permission state."""
    w = two_agent_world
    hdr = _hdr(w["mgr_a"]["token"], w["org_id"])
    case_id = _case(
        test_client, w["mgr_a"]["token"], w["org_id"], w["suite_id"], [w["ds_a"]["id"]]
    ).json()["id"]

    # A run needs a default model to launch; this test is about who may observe
    # it, not about what it produces.
    prov = test_client.post(
        "/api/llm/providers",
        json={
            "name": f"prov-{uuid.uuid4().hex[:6]}",
            "provider_type": "anthropic",
            "credentials": {"api_key": "dummy-key"},
            "models": [{"model_id": f"m-{uuid.uuid4().hex[:6]}", "name": "M", "is_custom": True}],
        },
        headers=_hdr(w["admin"]["token"], w["org_id"]),
    )
    assert prov.status_code == 200, prov.text
    model_id = prov.json()["models"][0]["id"]
    assert test_client.post(
        f"/api/llm/models/{model_id}/set_default?small=false",
        headers=_hdr(w["admin"]["token"], w["org_id"]),
    ).status_code == 200

    run = test_client.post(
        "/api/tests/runs", json={"case_ids": [case_id], "trigger_reason": "manual"},
        headers=hdr,
    )
    assert run.status_code == 200, run.text
    run_id = run.json()["id"]

    assert test_client.get(f"/api/tests/runs/{run_id}", headers=hdr).status_code == 200
    assert test_client.get(
        f"/api/tests/runs/{run_id}/status", headers=hdr
    ).status_code == 200

    # ...and B's manager, with no case in it, cannot observe that run at all.
    other = _hdr(w["mgr_b"]["token"], w["org_id"])
    assert test_client.get(f"/api/tests/runs/{run_id}", headers=other).status_code == 404
    assert test_client.get(
        f"/api/tests/runs/{run_id}/status", headers=other
    ).status_code == 404


@pytest.mark.e2e
def test_suite_counts_exclude_cases_the_caller_cannot_read(
    test_client, two_agent_world
):
    """Suite NAMES stay visible (the authoring dropdown needs them); the COUNTS
    must not disclose how many evals exist for agents you cannot see."""
    w = two_agent_world
    org = w["org_id"]
    for _ in range(3):
        _case(test_client, w["admin"]["token"], org, w["suite_id"], [w["ds_b"]["id"]])
    _case(test_client, w["admin"]["token"], org, w["suite_id"], [w["ds_a"]["id"]])

    summary = test_client.get(
        "/api/tests/suites/summary", headers=_hdr(w["mgr_a"]["token"], org)
    )
    assert summary.status_code == 200, summary.text
    row = next(s for s in summary.json() if s["id"] == w["suite_id"])
    assert row["tests_count"] == 1, \
        f"count must cover only readable cases, got {row['tests_count']}"
