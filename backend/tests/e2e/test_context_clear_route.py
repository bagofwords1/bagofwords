"""E2E: the composer's /clear through the real API.

POST /api/reports/{id}/context/clear starts a fresh agent context without
deleting anything: owner-only, idle-only (409 while a run is active, like
/compact), and visible afterwards in the transcript payload as a clear
boundary the divider anchors on.
"""
import asyncio
import uuid

import pytest

from tests.e2e.test_context_compact_route import _headers, _owner_and_member


def _run(coro):
    return asyncio.run(coro)


async def _seed_turns(report_id, user_id, n=4):
    from app.dependencies import async_session_maker
    from app.models.completion import Completion
    ids = []
    async with async_session_maker() as db:
        for i in range(n):
            role = "user" if i % 2 == 0 else "system"
            c = Completion(
                prompt={"content": f"ask {i}"} if role == "user" else {},
                completion={"content": f"answer {i}"} if role == "system" else {},
                status="success", model="test", role=role, message_type="ai_completion",
                report_id=report_id, user_id=user_id if role == "user" else None,
            )
            db.add(c)
            await db.commit()
            ids.append(str(c.id))
    return ids


async def _start_run(report_id, completion_id, user_id, org_id):
    from app.dependencies import async_session_maker
    from app.models.agent_execution import AgentExecution
    async with async_session_maker() as db:
        db.add(AgentExecution(
            completion_id=completion_id, organization_id=org_id, user_id=user_id,
            report_id=report_id, status="in_progress",
        ))
        await db.commit()


def _completion_count(test_client, report_id, token, org_id):
    resp = test_client.get(f"/api/reports/{report_id}/completions", headers=_headers(token, org_id))
    assert resp.status_code == 200, resp.json()
    return resp.json()


@pytest.mark.e2e
def test_clear_sets_a_boundary_without_deleting_turns(create_user, login_user, whoami, test_client, create_report):
    owner, _, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Clear", user_token=owner, org_id=org_id, data_sources=[])
    ids = _run(_seed_turns(report["id"], user_id))
    before = _completion_count(test_client, report["id"], owner, org_id)["total_completions"]

    resp = test_client.post(f"/api/reports/{report['id']}/context/clear", headers=_headers(owner, org_id))
    assert resp.status_code == 200, resp.json()
    data = resp.json()
    assert data["status"] == "cleared"
    assert data["covers_until_completion_id"] == ids[-1]
    assert data["clear_boundaries"][-1]["anchor_completion_id"] == ids[-1]

    listing = _completion_count(test_client, report["id"], owner, org_id)
    assert listing["total_completions"] == before
    assert listing["compaction"]["clear_boundaries"][-1]["completion_id"] == ids[-1]

    summary = test_client.get(f"/api/reports/{report['id']}/context/summary", headers=_headers(owner, org_id)).json()
    assert summary["summary_text"] == ""

    again = test_client.post(f"/api/reports/{report['id']}/context/clear", headers=_headers(owner, org_id))
    assert again.status_code == 200 and again.json()["status"] == "nothing_to_clear"


@pytest.mark.e2e
def test_clear_is_blocked_while_a_run_is_active(create_user, login_user, whoami, test_client, create_report):
    owner, _, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Clear busy", user_token=owner, org_id=org_id, data_sources=[])
    ids = _run(_seed_turns(report["id"], user_id))
    _run(_start_run(report["id"], ids[-1], user_id, org_id))

    resp = test_client.post(f"/api/reports/{report['id']}/context/clear", headers=_headers(owner, org_id))
    assert resp.status_code == 409, resp.json()
    listing = _completion_count(test_client, report["id"], owner, org_id)
    assert listing["compaction"]["clear_boundaries"] == []


@pytest.mark.e2e
def test_clear_is_owner_only(create_user, login_user, whoami, test_client, create_report):
    owner, member, org_id = _owner_and_member(create_user, login_user, whoami, test_client)
    user_id = whoami(owner)["id"]
    report = create_report(title="Clear access", user_token=owner, org_id=org_id, data_sources=[])
    _run(_seed_turns(report["id"], user_id))

    resp = test_client.post(f"/api/reports/{report['id']}/context/clear", headers=_headers(member, org_id))
    assert resp.status_code == 403
    resp = test_client.post(f"/api/reports/{uuid.uuid4()}/context/clear", headers=_headers(owner, org_id))
    assert resp.status_code in (403, 404)
