"""Eval authority: one bar for seeing, running and editing a case.

  - **Authority over EVERY agent a case targets.** A routing eval spanning
    agents A and B belongs to whoever manages both; a manager of A alone neither
    sees nor touches it.
  - **An agent-less case covers every agent**, so it is org-level only — to see
    as much as to change.
  - Evals are deliberately stricter than instructions, which grant read on a
    union. An instruction CHANGES your agent's behaviour so you must see it; an
    eval only tests, and its results carry real query output.
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


# ── one bar: authority over every agent a case targets ───────────────


@pytest.mark.e2e
def test_routing_eval_belongs_only_to_whoever_manages_both_agents(
    test_client, two_agent_world
):
    """A case spanning A and B is neither visible nor editable to a manager of
    only one of them. Seeing and changing an eval are the same bar: authority
    over EVERY agent it targets. An eval that verifies routing between two
    agents belongs to whoever manages both — a partial manager could not act on
    it anyway, and its results carry the other agent's query output."""
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
        assert case_id not in {c["id"] for c in listed.json()}, \
            "managing one of the two agents is not authority over the case"

        edited = test_client.patch(
            f"/api/tests/cases/{case_id}",
            json={"name": "hijacked"}, headers=_hdr(mgr["token"], w["org_id"]),
        )
        assert edited.status_code == 403

    # The admin manages both, so it is theirs.
    assert case_id in {
        c["id"] for c in test_client.get(
            "/api/tests/cases?limit=500", headers=_hdr(w["admin"]["token"], w["org_id"])
        ).json()
    }


@pytest.mark.e2e
def test_single_agent_case_is_hidden_from_the_other_manager(
    test_client, two_agent_world
):
    """A case targeting only A is invisible to B's manager, and visible to A's."""
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
def test_global_case_is_org_level_only(test_client, two_agent_world):
    """An agent-less case implicitly covers EVERY agent, so it takes org-level
    manage_evals — to see it as much as to change it. A per-agent grant, however
    many agents it covers, is never authority over all of them."""
    w = two_agent_world
    created = _case(test_client, w["admin"]["token"], w["org_id"], w["suite_id"], [])
    assert created.status_code == 200, created.text
    case_id = created.json()["id"]

    listed = test_client.get(
        "/api/tests/cases?limit=500", headers=_hdr(w["mgr_a"]["token"], w["org_id"])
    )
    assert case_id not in {c["id"] for c in listed.json()}

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

    # Homed on the agent they manage. An org-wide shelf (no data_source_id)
    # would be org-level — see test_org_wide_suite_requires_org_level_authority.
    mine = test_client.post(
        "/api/tests/suites",
        json={"name": f"mine_{uuid.uuid4().hex[:6]}", "data_source_id": w["ds_a"]["id"]},
        headers=_hdr(tok, org),
    )
    assert mine.status_code == 200, mine.text
    mine_id = mine.json()["id"]

    ids = [
        _case(test_client, tok, org, mine_id, [w["ds_a"]["id"]]).json()["id"]
        for _ in range(3)
    ]

    other = test_client.post(
        "/api/tests/suites",
        json={"name": f"other_{uuid.uuid4().hex[:6]}", "data_source_id": w["ds_a"]["id"]},
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
        "/api/tests/suites",
        json={"name": f"shared_{uuid.uuid4().hex[:6]}", "data_source_id": w["ds_a"]["id"]},
        headers=_hdr(w["mgr_a"]["token"], org),
    ).json()["id"]

    a_case = _case(test_client, w["mgr_a"]["token"], org, shared, [w["ds_a"]["id"]]).json()["id"]
    b_case = _case(test_client, w["mgr_b"]["token"], org, shared, [w["ds_b"]["id"]]).json()["id"]

    deleted = test_client.delete(
        f"/api/tests/suites/{shared}", headers=_hdr(w["mgr_a"]["token"], org)
    )
    assert deleted.status_code == 200, deleted.text
    # The delete is PARTIAL, and must say so — a caller told only "deleted"
    # would believe the suite's whole contents went with it.
    assert deleted.json().get("reparented") == 1, deleted.json()

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


# ── org-wide shelves are org-level, however empty ────────────────────


@pytest.mark.e2e
def test_org_wide_suite_requires_org_level_authority(test_client, two_agent_world):
    """A suite with no home agent holds the cases that run against EVERY agent,
    so creating, renaming and deleting one is org-level — the same bar as
    authoring an agent-less case.

    The empty case is the one that bites: suite authority otherwise derives from
    the cases held, and an EMPTY org-wide shelf has none to fail on, so a
    per-agent manager could rename or delete it.
    """
    w = two_agent_world
    mgr, org = w["mgr_a"]["token"], w["org_id"]

    # A per-agent manager cannot open an org-wide shelf...
    assert test_client.post(
        "/api/tests/suites", json={"name": f"orgwide_{uuid.uuid4().hex[:6]}"},
        headers=_hdr(mgr, org),
    ).status_code == 403

    # ...but can still make one homed on the agent they manage.
    assert test_client.post(
        "/api/tests/suites",
        json={"name": f"mine_{uuid.uuid4().hex[:6]}", "data_source_id": w["ds_a"]["id"]},
        headers=_hdr(mgr, org),
    ).status_code == 200

    # An admin's EMPTY org-wide shelf stays out of reach.
    orgwide = test_client.post(
        "/api/tests/suites", json={"name": f"shelf_{uuid.uuid4().hex[:6]}"},
        headers=_hdr(w["admin"]["token"], org),
    )
    assert orgwide.status_code == 200, orgwide.text
    sid = orgwide.json()["id"]
    assert orgwide.json()["data_source_id"] is None

    assert test_client.patch(
        f"/api/tests/suites/{sid}", json={"name": "renamed"}, headers=_hdr(mgr, org)
    ).status_code == 403
    assert test_client.delete(
        f"/api/tests/suites/{sid}", headers=_hdr(mgr, org)
    ).status_code == 403

    # The org admin may.
    assert test_client.delete(
        f"/api/tests/suites/{sid}", headers=_hdr(w["admin"]["token"], org)
    ).status_code == 200


@pytest.mark.e2e
def test_suites_list_separates_agent_shelves_from_org_wide(test_client, two_agent_world):
    """The tree asks for one scope at a time: an agent's shelves, or the
    org-wide ones. Neither query returns the other's."""
    w = two_agent_world
    org, tok = w["org_id"], w["admin"]["token"]
    mine = test_client.post(
        "/api/tests/suites",
        json={"name": f"agent_{uuid.uuid4().hex[:6]}", "data_source_id": w["ds_a"]["id"]},
        headers=_hdr(tok, org),
    ).json()["id"]
    glob = test_client.post(
        "/api/tests/suites", json={"name": f"glob_{uuid.uuid4().hex[:6]}"},
        headers=_hdr(tok, org),
    ).json()["id"]

    agent_shelf = {s["id"] for s in test_client.get(
        f"/api/tests/suites?limit=100&data_source_id={w['ds_a']['id']}", headers=_hdr(tok, org)
    ).json()}
    org_shelf = {s["id"] for s in test_client.get(
        "/api/tests/suites?limit=100&scope=global", headers=_hdr(tok, org)
    ).json()}

    assert mine in agent_shelf and mine not in org_shelf
    assert glob in org_shelf and glob not in agent_shelf
