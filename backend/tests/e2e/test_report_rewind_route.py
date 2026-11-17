"""E2E: Rewind through the real API (BOW-145).

POST /api/reports/{id}/rewind {completion_id} removes the chosen prompt and
every later turn: gone from the timeline (and after a reload), owner-only,
idle-only (409 while a run is active), and only a prompt the user sent can be
the rewind point.
"""
import uuid

import pytest

from tests.e2e.test_context_clear_route import _run, _seed_turns, _start_run
from tests.e2e.test_context_compact_route import _headers, _owner_and_member


def _timeline(test_client, report_id, token, org_id):
    resp = test_client.get(f"/api/reports/{report_id}/completions", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _rewind(test_client, report_id, completion_id, token, org_id):
    return test_client.post(
        f"/api/reports/{report_id}/rewind",
        json={"completion_id": completion_id},
        headers=_headers(token, org_id),
    )


@pytest.mark.e2e
@pytest.mark.parametrize("n_turns,rewind_at", [(4, 0), (6, 2), (8, 6)])
def test_rewind_removes_later_turns_from_the_timeline(create_user, login_user, whoami, test_client, create_report, n_turns, rewind_at):
    owner, _, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Rewind", user_token=owner, org_id=org_id, data_sources=[])
    ids = _run(_seed_turns(report["id"], user_id, n=n_turns))

    resp = _rewind(test_client, report["id"], ids[rewind_at], owner, org_id)
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["status"] == "rewound"
    assert data["removed_count"] == n_turns - rewind_at
    assert data["prompt"] == f"ask {rewind_at}"

    # A fresh read (what a reload does) no longer has the removed turns.
    listing = _timeline(test_client, report["id"], owner, org_id)
    visible = [c["id"] for c in listing["completions"]]
    assert set(visible) == set(ids[:rewind_at])
    assert listing["total_completions"] == rewind_at

    # The removed prompt cannot be rewound to again.
    again = _rewind(test_client, report["id"], ids[rewind_at], owner, org_id)
    assert again.status_code == 404


@pytest.mark.e2e
def test_rewind_is_blocked_while_a_run_is_active(create_user, login_user, whoami, test_client, create_report):
    owner, _, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Rewind busy", user_token=owner, org_id=org_id, data_sources=[])
    ids = _run(_seed_turns(report["id"], user_id))
    _run(_start_run(report["id"], ids[-1], user_id, org_id))

    resp = _rewind(test_client, report["id"], ids[0], owner, org_id)
    assert resp.status_code == 409, resp.json()
    assert _timeline(test_client, report["id"], owner, org_id)["total_completions"] == len(ids)


@pytest.mark.e2e
def test_only_a_sent_prompt_can_be_rewound_to(create_user, login_user, whoami, test_client, create_report):
    owner, _, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Rewind reply", user_token=owner, org_id=org_id, data_sources=[])
    ids = _run(_seed_turns(report["id"], user_id))

    assert _rewind(test_client, report["id"], ids[1], owner, org_id).status_code == 400  # agent reply
    assert _rewind(test_client, report["id"], str(uuid.uuid4()), owner, org_id).status_code == 404
    assert _timeline(test_client, report["id"], owner, org_id)["total_completions"] == len(ids)


@pytest.mark.e2e
def test_rewind_is_owner_only(create_user, login_user, whoami, test_client, create_report):
    owner, member, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Rewind access", user_token=owner, org_id=org_id, data_sources=[])
    ids = _run(_seed_turns(report["id"], user_id))

    assert _rewind(test_client, report["id"], ids[0], member, org_id).status_code == 403
    assert _timeline(test_client, report["id"], owner, org_id)["total_completions"] == len(ids)
