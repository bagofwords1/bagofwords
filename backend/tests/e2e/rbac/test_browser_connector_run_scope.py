"""The browser connector follows the run's agents, in Auto and pinned.

Contract: browser tools may use a browser connection that belongs to an agent
the run executes against — the pinned agents, or in Auto every agent the
RUNNING user can access. Auto used to see no connector at all (it only read
the report's attachments); a pinned selection stays a hard scope, and a member
never reaches a browser agent they cannot access.

Real routes, services and the committed-policy check (fresh session); no
browser is launched — the policy decision is the contract.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User

pytestmark = pytest.mark.e2e


def _run(coro):
    return asyncio.run(coro)


async def _ctx(report_id, user_id, org_id):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        user = await db.get(User, user_id)
        organization = await db.get(Organization, org_id)
        assert report and user and organization
        return {"report": report, "user": user, "organization": organization}


@pytest.fixture
def org(bootstrap_admin, create_connection, create_domain_from_connection, create_data_source,
        invite_user_to_org, dynamic_sqlite_db):
    """Admin + member; a public and an admin-only browser agent, plus a query-only agent."""
    admin = bootstrap_admin("browserscope")
    token, org_id = admin["token"], admin["org_id"]
    agents = {}
    for label, public in (("shared", True), ("private", False)):
        conn = create_connection(
            name=f"{label} browser {uuid.uuid4().hex[:6]}", type="browser",
            config={"url_patterns": [f"https://{label}.example.test/**"], "allow_downloads": False},
            credentials={}, user_token=token, org_id=org_id,
        )
        ds = create_domain_from_connection(name=f"{label} web {uuid.uuid4().hex[:6]}", connection_id=conn["id"],
                                           user_token=token, org_id=org_id, is_public=public)
        agents[label] = {"ds_id": str(ds["id"]), "pattern": f"https://{label}.example.test/**"}
    query = create_data_source(name=f"orders {uuid.uuid4().hex[:6]}", type="sqlite",
                               config={"database": dynamic_sqlite_db}, user_token=token, org_id=org_id)
    agents["query"] = {"ds_id": str(query["id"])}
    member = invite_user_to_org(org_id=org_id, admin_token=token)
    return {"admin": admin, "member": member, "org_id": org_id, "agents": agents}


def _patterns(policy) -> set:
    """Every URL pattern of every browser connection the policy exposes."""
    import json

    out = set()
    for ds in getattr(policy.report, "data_sources", None) or []:
        for c in ds.connections:
            cfg = c.config
            while isinstance(cfg, str):
                cfg = json.loads(cfg)
            out |= set((cfg or {}).get("url_patterns") or [])
    return out


def _policy(create_report, whoami, org, who, data_sources):
    user = org[who]
    report = create_report(title="browser scope", user_token=user["token"], org_id=org["org_id"],
                           data_sources=data_sources)
    assert bool(report.get("data_sources")) == bool(data_sources)
    ctx = _run(_ctx(report["id"], whoami(user["token"])["id"], org["org_id"]))
    from app.services.artifact_verification_policy import browser_policy

    return _run(browser_policy(ctx)), ctx


@pytest.mark.parametrize("who", ["admin", "member"])
def test_auto_reaches_a_browser_agent_the_user_can_access(create_report, whoami, org, who):
    from app.services.artifact_verification_policy import require_browser_access

    policy, ctx = _policy(create_report, whoami, org, who, [])
    assert policy.connector is True
    assert org["agents"]["shared"]["pattern"] in _patterns(policy)
    if who == "admin":
        assert org["agents"]["private"]["pattern"] in _patterns(policy)
    assert _run(require_browser_access(ctx)).connector is True


def test_auto_member_never_reaches_a_browser_agent_they_cannot_access(create_report, whoami, org):
    policy, _ = _policy(create_report, whoami, org, "member", [])
    patterns = _patterns(policy)
    assert org["agents"]["shared"]["pattern"] in patterns
    assert org["agents"]["private"]["pattern"] not in patterns


def test_pinned_selection_without_a_browser_agent_has_no_connector(create_report, whoami, org):
    from app.services.artifact_verification_policy import require_browser_access

    policy, ctx = _policy(create_report, whoami, org, "admin", [org["agents"]["query"]["ds_id"]])
    assert policy.connector is False
    with pytest.raises(PermissionError):
        _run(require_browser_access(ctx))
