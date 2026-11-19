"""A pending tool approval can only be answered by the user who started the run,
through the authenticated completion route.

Contract:
* there is no unauthenticated way to settle a pending confirmation;
* on the completion route, a principal from another organization, or the run's
  own user presenting an organization they are not bound to, is refused (403)
  and the confirmation stays pending;
* the run's owner, in their organization, resolves it;
* a run's owner who is bound to several organizations answers only from the
  organization the run (its completion and confirmation) belongs to.
"""
import asyncio
import uuid

import pytest

from app.ai.tools.confirmation import (
    KIND_BUILTIN_TOOL, PENDING_CONFIRMATIONS, discard_confirmation, register_confirmation,
)

from datetime import datetime, timedelta

from app.dependencies import async_session_maker
from app.models.completion import Completion
from app.models.tool_confirmation import ToolConfirmation

from .agent_surface_fixtures import allow_multiple_orgs, h, two_orgs  # noqa: F401


async def _register(confirmation_id, meta):
    return register_confirmation(confirmation_id, meta)


@pytest.fixture
def pending(two_orgs):
    """A confirmation waiting on the `other` admin's run, registered the way a
    tool does mid-run (the in-memory future is the state under test)."""
    o = two_orgs
    completion_id, confirmation_id = str(uuid.uuid4()), str(uuid.uuid4())
    loop = asyncio.new_event_loop()
    future = loop.run_until_complete(_register(confirmation_id, {
        "kind": KIND_BUILTIN_TOOL, "user_id": o["other_user_id"],
        "completion_ids": [completion_id], "tool_name": "set_report_agents",
    }))
    try:
        yield {**o, "future": future, "confirmation_id": confirmation_id,
               "url": f"/api/completions/{completion_id}/mcp_tool_confirmations/{confirmation_id}"}
    finally:
        discard_confirmation(confirmation_id)
        loop.close()


@pytest.mark.parametrize("approved", [True, False])
def test_there_is_no_unauthenticated_confirmation_route(test_client, pending, approved):
    p = pending
    for headers in ({}, h(p["home_token"], p["home_org"])):
        r = test_client.post(f"/api/artifacts/confirm/{p['confirmation_id']}",
                             json={"approved": approved}, headers=headers)
        assert r.status_code in (404, 405), r.text
    assert not p["future"].done()
    assert p["confirmation_id"] in PENDING_CONFIRMATIONS


def test_only_the_run_owner_in_their_organization_can_answer(test_client, pending):
    p = pending
    body = {"approved": True}

    # No session at all.
    assert test_client.post(p["url"], json=body).status_code in (401, 403)
    # A principal of another organization.
    outsider = test_client.post(p["url"], json=body, headers=h(p["home_token"], p["home_org"]))
    assert outsider.status_code == 403, outsider.text
    # The same principal presenting the run's organization, which they are not bound to.
    spoofed = test_client.post(p["url"], json=body, headers=h(p["home_token"], p["other_org"]))
    assert spoofed.status_code == 403, spoofed.text
    assert not p["future"].done()

    owner = test_client.post(p["url"], json=body, headers=h(p["other_token"], p["other_org"]))
    assert owner.status_code == 200, owner.text
    assert p["future"].result()["approved"] is True


async def _seed_run_confirmation(report_id, org_id, user_id, *, with_row_org):
    """A system completion with a pending confirmation row, as a running tool
    leaves them (agent-loop state, so written directly)."""
    async with async_session_maker() as db:
        completion = Completion(prompt={"content": "x"}, completion={"content": ""}, role="system",
                                status="in_progress", report_id=report_id, user_id=user_id)
        db.add(completion)
        await db.flush()
        row = ToolConfirmation(
            id=str(uuid.uuid4()), kind=KIND_BUILTIN_TOOL, status=ToolConfirmation.STATUS_PENDING,
            organization_id=org_id if with_row_org else None, report_id=report_id,
            system_completion_id=str(completion.id), user_id=user_id, tool_name="set_report_agents",
            arguments={}, remember=False, expires_at=datetime.utcnow() + timedelta(seconds=240),
        )
        db.add(row)
        await db.commit()
        return str(completion.id), str(row.id)


async def _status(confirmation_id):
    async with async_session_maker() as db:
        return (await db.get(ToolConfirmation, confirmation_id)).status


# with_row_org=False: the completion's own organization must decide even for a
# confirmation row that carries none.
@pytest.mark.parametrize("with_row_org", [True, False])
def test_member_of_two_organizations_answers_only_from_the_runs_organization(
    test_client, two_orgs, create_report, with_row_org,
):
    o = two_orgs
    # The `other` admin is bound to both organizations; the run is in `other`.
    report = create_report(title="run", user_token=o["other_token"], org_id=o["other_org"])
    completion_id, confirmation_id = asyncio.run(_seed_run_confirmation(
        report["id"], o["other_org"], o["other_user_id"], with_row_org=with_row_org))
    url = f"/api/completions/{completion_id}/mcp_tool_confirmations/{confirmation_id}"

    wrong_org = test_client.post(url, json={"approved": True}, headers=h(o["other_token"], o["home_org"]))
    assert wrong_org.status_code in (403, 404), wrong_org.text
    assert asyncio.run(_status(confirmation_id)) == ToolConfirmation.STATUS_PENDING

    right_org = test_client.post(url, json={"approved": True}, headers=h(o["other_token"], o["other_org"]))
    assert right_org.status_code == 200, right_org.text
    assert right_org.json()["approved"] is True
    assert asyncio.run(_status(confirmation_id)) != ToolConfirmation.STATUS_PENDING
