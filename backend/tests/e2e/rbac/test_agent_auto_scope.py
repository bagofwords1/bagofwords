"""Auto is a MODE, not a materialized set.

"Agent" == ``DataSource``. A report's attachments encode the scope:

  - attached agents  → a hard manual pin; the run uses exactly those,
  - no attachments   → **Auto**; the working set is resolved at run time to
    every agent the RUNNING user can access, so it follows access changes and
    picks up agents created after the report.

The bug this pins: the working set was read straight off ``report.data_sources``
at every AgentV2 construction site, so an Auto report executed against nothing —
no schema in context, no query clients. The UI hid that hole by materializing
Auto into a full-roster pin before saving, which also froze the roster and made
"I picked every agent" indistinguishable from "I picked none".
"""
import uuid

import pytest


def _auth(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _create_report(test_client, token, org_id, data_sources):
    resp = test_client.post(
        "/api/reports",
        json={"title": f"r_{uuid.uuid4().hex[:6]}", "data_sources": data_sources},
        headers=_auth(token, org_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _load_report(session, report_id):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from app.models.report import Report

    return (
        await session.execute(
            select(Report)
            .options(selectinload(Report.data_sources))
            .where(Report.id == str(report_id))
        )
    ).scalar_one()


async def _resolved_ids(session, org_id, user_id, report_id):
    from app.models.organization import Organization
    from app.models.user import User
    from app.ai.tools.implementations.agent_focus_common import resolve_run_agents

    org = await session.get(Organization, str(org_id))
    user = await session.get(User, str(user_id))
    report = await _load_report(session, report_id)
    agents = await resolve_run_agents(session, org, user, report)
    return {str(a.id) for a in agents}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_unattached_report_resolves_to_accessible_agents(
    bootstrap_admin, sqlite_data_source, test_client
):
    """Auto: no attachments → the run works against what the user can access,
    and an agent created later joins without the report being touched. A
    materialized pin cannot do that."""
    from app.dependencies import async_session_maker

    admin = bootstrap_admin("autoscope")
    first = sqlite_data_source(
        name=f"auto_a_{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=admin["org_id"]
    )
    report_id = _create_report(test_client, admin["token"], admin["org_id"], [])

    async with async_session_maker() as session:
        report = await _load_report(session, report_id)
        assert report.data_sources == [], "an empty selection must persist as Auto"
        assert await _resolved_ids(
            session, admin["org_id"], admin["user_id"], report_id
        ) == {str(first["id"])}

    second = sqlite_data_source(
        name=f"auto_b_{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=admin["org_id"]
    )
    async with async_session_maker() as session:
        assert await _resolved_ids(
            session, admin["org_id"], admin["user_id"], report_id
        ) == {str(first["id"]), str(second["id"])}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_pinned_report_is_a_hard_scope(
    bootstrap_admin, sqlite_data_source, test_client
):
    """Manual: the pin is honored verbatim and never widened to the roster —
    including when the pin is the org's ONLY agent, which must not read back as
    Auto just because the two sets currently coincide."""
    from app.dependencies import async_session_maker
    from app.ai.tools.implementations.agent_focus_common import report_selection_is_auto

    admin = bootstrap_admin("pinscope")
    pinned = sqlite_data_source(
        name=f"pin_a_{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=admin["org_id"]
    )
    report_id = _create_report(
        test_client, admin["token"], admin["org_id"], [str(pinned["id"])]
    )

    async with async_session_maker() as session:
        report = await _load_report(session, report_id)
        assert report_selection_is_auto(report) is False
        assert await _resolved_ids(
            session, admin["org_id"], admin["user_id"], report_id
        ) == {str(pinned["id"])}

    other = sqlite_data_source(
        name=f"pin_b_{uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=admin["org_id"]
    )
    async with async_session_maker() as session:
        resolved = await _resolved_ids(
            session, admin["org_id"], admin["user_id"], report_id
        )
        assert resolved == {str(pinned["id"])}
        assert str(other["id"]) not in resolved


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_auto_is_scoped_to_the_running_user(
    bootstrap_admin, invite_user_to_org, sqlite_data_source, test_client
):
    """Auto resolves per user, not per org: "every agent I can access" can never
    hand someone an agent they have no access to."""
    from app.dependencies import async_session_maker

    admin = bootstrap_admin("autouser")
    private = sqlite_data_source(
        name=f"private_{uuid.uuid4().hex[:4]}",
        user_token=admin["token"],
        org_id=admin["org_id"],
        is_public=False,
    )
    report_id = _create_report(test_client, admin["token"], admin["org_id"], [])
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])

    async with async_session_maker() as session:
        assert str(private["id"]) in await _resolved_ids(
            session, admin["org_id"], admin["user_id"], report_id
        )
        assert str(private["id"]) not in await _resolved_ids(
            session, admin["org_id"], member["user_id"], report_id
        )
