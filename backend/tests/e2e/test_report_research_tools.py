"""E2E tests for the report research tools: search_reports and read_report.

These planner tools let the agent discover and read the CURRENT USER's own
reports. The key contract is user-scoping: a user can only ever see/read their
own reports, never another user's — even within the same organization.

We exercise the tools directly via run_stream against the real test DB,
using a SimpleNamespace for the {user, organization} runtime context (the
tools only read `.id` off them).
"""
import uuid
from types import SimpleNamespace

import pytest


def _setup_two_users(create_user, login_user, whoami, test_client):
    """Two users in the same org. Returns (token1, uid1, token2, uid2, org_id)."""
    email1 = f"rt_owner_{uuid.uuid4().hex[:6]}@test.com"
    create_user(email=email1, password="test123")
    token1 = login_user(email=email1, password="test123")
    me1 = whoami(token1)
    org_id = me1["organizations"][0]["id"]
    uid1 = me1["id"]

    email2 = f"rt_member_{uuid.uuid4().hex[:6]}@test.com"
    test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": email2, "role": "member"},
        headers={"Authorization": f"Bearer {token1}", "X-Organization-Id": org_id},
    )
    create_user(email=email2, password="test123")
    token2 = login_user(email=email2, password="test123")
    me2 = whoami(token2)
    uid2 = me2["id"]

    return token1, uid1, token2, uid2, org_id


async def _run(tool, tool_input, *, user_id, org_id):
    from app.dependencies import async_session_maker

    async with async_session_maker() as db:
        ctx = {
            "db": db,
            "user": SimpleNamespace(id=user_id),
            "organization": SimpleNamespace(id=org_id),
        }
        end = None
        async for evt in tool.run_stream(tool_input, ctx):
            if evt.type == "tool.end":
                end = evt
        assert end is not None, "expected a tool.end event"
        return end.payload["output"]


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_search_reports_is_user_scoped(
    create_user, login_user, whoami, test_client, create_report,
):
    from app.ai.tools.implementations.search_reports import SearchReportsTool

    token1, uid1, token2, uid2, org_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    # u1 owns two reports; u2 owns one with an overlapping keyword.
    create_report(title="Revenue dashboard", user_token=token1, org_id=org_id, data_sources=[])
    create_report(title="Churn study", user_token=token1, org_id=org_id, data_sources=[])
    create_report(title="Revenue secret (u2)", user_token=token2, org_id=org_id, data_sources=[])

    tool = SearchReportsTool()

    # List all -> only u1's two reports, never u2's.
    out = await _run(tool, {}, user_id=uid1, org_id=org_id)
    assert out["success"] is True
    titles = sorted(r["title"] for r in out["reports"])
    assert titles == ["Churn study", "Revenue dashboard"], titles

    # Search "revenue" -> u1 sees only their own revenue report.
    out = await _run(tool, {"query": "revenue"}, user_id=uid1, org_id=org_id)
    titles = [r["title"] for r in out["reports"]]
    assert titles == ["Revenue dashboard"], titles

    # u2 searching "revenue" sees only their own.
    out = await _run(tool, {"query": "revenue"}, user_id=uid2, org_id=org_id)
    titles = [r["title"] for r in out["reports"]]
    assert titles == ["Revenue secret (u2)"], titles


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_read_report_user_scoping(
    create_user, login_user, whoami, test_client, create_report,
):
    from app.ai.tools.implementations.read_report import ReadReportTool

    token1, uid1, token2, uid2, org_id = _setup_two_users(
        create_user, login_user, whoami, test_client
    )

    r1 = create_report(title="My report", user_token=token1, org_id=org_id, data_sources=[])
    r2 = create_report(title="Their report", user_token=token2, org_id=org_id, data_sources=[])

    tool = ReadReportTool()

    # Owner can read their report.
    out = await _run(tool, {"report_id": r1["id"]}, user_id=uid1, org_id=org_id)
    assert out["success"] is True
    assert out["title"] == "My report"
    assert isinstance(out["artifacts"], list)
    assert isinstance(out["conversation"], list)

    # u1 cannot read u2's report — indistinguishable not_found, no leak.
    out = await _run(tool, {"report_id": r2["id"]}, user_id=uid1, org_id=org_id)
    assert out["success"] is False
    assert out["error"] == "not_found"
    assert out["title"] is None

    # u2 can read their own.
    out = await _run(tool, {"report_id": r2["id"]}, user_id=uid2, org_id=org_id)
    assert out["success"] is True
    assert out["title"] == "Their report"

    # Unknown id -> not_found.
    out = await _run(tool, {"report_id": str(uuid.uuid4())}, user_id=uid1, org_id=org_id)
    assert out["success"] is False
    assert out["error"] == "not_found"
