"""E2E: Rewind through the real API.

POST /api/reports/{id}/rewind removes a prompt and every later turn: they are
gone from the transcript (and stay gone on reload), the removed prompt comes
back for editing, the route is owner-only, and it is idle-only (409 while a
run is active, like /compact and /clear).
"""
import asyncio
import uuid

import pytest

from tests.e2e.test_context_clear_route import _seed_turns, _start_run
from tests.e2e.test_context_compact_route import _headers, _owner_and_member


def _run(coro):
    return asyncio.run(coro)


def _transcript_ids(test_client, report_id, token, org_id):
    resp = test_client.get(f"/api/reports/{report_id}/completions?limit=50", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return [c["id"] for c in resp.json()["completions"]]


@pytest.mark.e2e
@pytest.mark.parametrize("n_turns,target", [(4, 2), (8, 0), (8, 4)])
def test_rewind_removes_later_turns_from_the_transcript(create_user, login_user, whoami, test_client, create_report, n_turns, target):
    owner, _, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Rewind", user_token=owner, org_id=org_id, data_sources=[])
    ids = _run(_seed_turns(report["id"], user_id, n=n_turns))
    assert _transcript_ids(test_client, report["id"], owner, org_id) == ids

    resp = test_client.post(
        f"/api/reports/{report['id']}/rewind",
        json={"completion_id": ids[target]},
        headers=_headers(owner, org_id),
    )
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["removed_count"] == n_turns - target
    assert set(data["removed_ids"]) == set(ids[target:])
    assert data["prompt"]["content"] == f"ask {target}"

    # Gone after a reload, too.
    assert _transcript_ids(test_client, report["id"], owner, org_id) == ids[:target]

    # The rewound prompt cannot be rewound to again.
    again = test_client.post(
        f"/api/reports/{report['id']}/rewind",
        json={"completion_id": ids[target]},
        headers=_headers(owner, org_id),
    )
    assert again.status_code == 404
    assert again.json()["error_code"] == "rewind.target_not_found"


@pytest.mark.e2e
def test_rewind_is_blocked_while_a_run_is_active(create_user, login_user, whoami, test_client, create_report):
    owner, _, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Rewind busy", user_token=owner, org_id=org_id, data_sources=[])
    ids = _run(_seed_turns(report["id"], user_id))
    _run(_start_run(report["id"], ids[-1], user_id, org_id))

    resp = test_client.post(
        f"/api/reports/{report['id']}/rewind",
        json={"completion_id": ids[0]},
        headers=_headers(owner, org_id),
    )
    assert resp.status_code == 409, resp.json()
    assert resp.json()["error_code"] == "rewind.run_in_progress"
    assert _transcript_ids(test_client, report["id"], owner, org_id) == ids


@pytest.mark.e2e
def test_rewind_only_targets_prompts(create_user, login_user, whoami, test_client, create_report):
    owner, _, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Rewind target", user_token=owner, org_id=org_id, data_sources=[])
    ids = _run(_seed_turns(report["id"], user_id))

    resp = test_client.post(
        f"/api/reports/{report['id']}/rewind",
        json={"completion_id": ids[1]},  # an answer
        headers=_headers(owner, org_id),
    )
    assert resp.status_code == 400
    assert resp.json()["error_code"] == "rewind.invalid_target"
    assert _transcript_ids(test_client, report["id"], owner, org_id) == ids


@pytest.mark.e2e
def test_rewind_is_owner_only(create_user, login_user, whoami, test_client, create_report):
    owner, member, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Rewind access", user_token=owner, org_id=org_id, data_sources=[])
    ids = _run(_seed_turns(report["id"], user_id))

    resp = test_client.post(
        f"/api/reports/{report['id']}/rewind",
        json={"completion_id": ids[0]},
        headers=_headers(member, org_id),
    )
    assert resp.status_code == 403
    assert _transcript_ids(test_client, report["id"], owner, org_id) == ids
    resp = test_client.post(
        f"/api/reports/{uuid.uuid4()}/rewind",
        json={"completion_id": ids[0]},
        headers=_headers(owner, org_id),
    )
    assert resp.status_code in (403, 404)
