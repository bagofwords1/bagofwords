"""Prompt run + run-for: self-run report creation, run-for fan-out with
access filtering + per-user private reports, and the Step-0 privacy invariant
(an admin cannot read a target's run-for report).

CompletionService.create_completion is stubbed via monkeypatch so no LLM is
invoked — we only assert the report/prompt_run bookkeeping the endpoint owns.

Run:
    cd backend
    BOW_DATABASE_URL=sqlite:///db/app.db \
      .venv/bin/python -m pytest tests/prompts/test_prompt_run.py -p no:warnings -q
"""
import uuid
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.user import User
from app.models.data_source import DataSource
from app.models.resource_grant import ResourceGrant
from app.models.membership import Membership
from app.models.role import Role
from app.models.role_assignment import RoleAssignment
from app.models.report import Report
from app.models.prompt_run import PromptRun
from app.models.group import Group
from app.models.group_membership import GroupMembership

from app.services.prompt_service import prompt_service, PromptService
from app.services.report_service import ReportService
from app.schemas.prompt_schema import PromptCreate


def _u(suffix, name):
    return User(name=f"{name} {suffix}", email=f"{name.lower()}-{suffix}@example.com",
                hashed_password="x", is_active=True, is_verified=True)


async def _grant(db, org_id, ds_id, user_id, perms):
    db.add(ResourceGrant(
        organization_id=org_id, resource_type="data_source", resource_id=ds_id,
        principal_type="user", principal_id=user_id, permissions=perms,
    ))


async def _seed():
    """admin (full_admin) + two members; one agent (ds1). admin manages it."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Run Org {suffix}")
        db.add(org); await db.flush()
        admin = _u(suffix, "Admin")
        m_yes = _u(suffix, "MemberYes")   # will get access to the agent
        m_no = _u(suffix, "MemberNo")     # no access → should be skipped
        db.add_all([admin, m_yes, m_no]); await db.flush()
        for u, role in ((admin, "admin"), (m_yes, "member"), (m_no, "member")):
            db.add(Membership(user_id=u.id, organization_id=org.id, role=role))
        r = Role(organization_id=org.id, name=f"Admin {suffix}", permissions=["full_admin_access"], is_system=False)
        db.add(r); await db.flush()
        db.add(RoleAssignment(organization_id=org.id, role_id=r.id, principal_type="user", principal_id=admin.id))
        ds1 = DataSource(name=f"A1 {suffix}", organization_id=org.id, is_active=True, owner_user_id=admin.id)
        db.add(ds1); await db.flush()
        await _grant(db, org.id, ds1.id, admin.id, ["view", "manage"])
        await _grant(db, org.id, ds1.id, m_yes.id, ["view"])
        await db.commit()
        return {"org": org.id, "admin": admin.id, "m_yes": m_yes.id, "m_no": m_no.id, "ds1": ds1.id}


def _stub_completion(monkeypatch):
    """Replace CompletionService.create_completion with a no-op recorder so no
    LLM/model resolution runs. Returns the list of (report_id, user_id) calls."""
    calls = []

    async def _fake(self, db, report_id, completion_data, current_user, organization, *a, **kw):
        calls.append((str(report_id), str(current_user.id), completion_data.prompt.content))
        return {"stubbed": True}

    from app.services.completion_service import CompletionService
    monkeypatch.setattr(CompletionService, "create_completion", _fake)
    return calls


@pytest.mark.asyncio
async def test_substitute_semantics():
    s = PromptService.substitute
    assert s("Hi {{name}}", {"name": "Sam"}) == "Hi Sam"
    assert s("{{region}} for {{period}}", {"region": "EMEA", "period": {"start": "Jan", "end": "Mar"}}) == "EMEA for Jan to Mar"
    assert s("X {{missing}} Y", {}) == "X  Y"            # missing → empty
    assert s("{{r}}", {"r": {"start": "", "end": ""}}) == ""  # empty range → empty


@pytest.mark.asyncio
async def test_run_self_creates_report_and_records_run(monkeypatch):
    calls = _stub_completion(monkeypatch)
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])

        p = await prompt_service.create_prompt(
            db, PromptCreate(title="Self", text="Summarize {{region}}", scope="agent",
                             mode="deep", data_source_ids=[ids["ds1"]]), admin, org)

        out = await prompt_service.run_prompt(
            db, p.id, admin, org, parameters={"region": "AMER"})
        assert "report_id" in out
        rid = out["report_id"]

        # report created, owned by caller, private, mode carried from the prompt
        report = (await db.execute(select(Report).filter(Report.id == rid))).scalar_one()
        assert str(report.user_id) == str(admin.id)
        assert (report.artifact_visibility or "none") == "none"
        assert report.mode == "deep"

        # completion was invoked as the caller with the substituted text
        assert calls and calls[-1][1] == str(admin.id)
        assert "AMER" in calls[-1][2] and "{{" not in calls[-1][2]

        # prompt_run recorded (actor == user == caller)
        runs = (await db.execute(select(PromptRun).filter(PromptRun.report_id == rid))).scalars().all()
        assert len(runs) == 1
        assert str(runs[0].actor_id) == str(admin.id) == str(runs[0].user_id)
        assert runs[0].parameters == {"region": "AMER"}
        print("[run] self-run creates private report + records prompt_run")


@pytest.mark.asyncio
async def test_run_self_denied_when_not_visible(monkeypatch):
    _stub_completion(monkeypatch)
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        m_no = await db.get(User, ids["m_no"])
        p = await prompt_service.create_prompt(
            db, PromptCreate(title="Priv", text="x", scope="agent",
                             data_source_ids=[ids["ds1"]]), admin, org)
        denied = False
        try:
            await prompt_service.run_prompt(db, p.id, m_no, org, parameters={})
        except HTTPException as e:
            denied = e.status_code == 403
        assert denied
        print("[run] caller without access is 403")


@pytest.mark.asyncio
async def test_run_for_fanout_filters_and_private_reports(monkeypatch):
    calls = _stub_completion(monkeypatch)
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])

        p = await prompt_service.create_prompt(
            db, PromptCreate(title="ForAll", text="Run {{x}}", scope="agent",
                             data_source_ids=[ids["ds1"]]), admin, org)

        out = await prompt_service.run_prompt_for(
            db, p.id, admin, org, principal_type="users",
            user_ids=[ids["m_yes"], ids["m_no"]], parameters={"x": "1"})

        # m_yes runs; m_no skipped (no agent access)
        assert out["ran"] == 1
        assert out["skipped"] == 1
        assert ids["m_no"] in out["skipped_user_ids"]

        # exactly one report, owned by + private to m_yes, run AS m_yes
        runs = (await db.execute(select(PromptRun).filter(PromptRun.prompt_id == p.id))).scalars().all()
        assert len(runs) == 1
        run = runs[0]
        assert str(run.user_id) == str(ids["m_yes"])
        assert str(run.actor_id) == str(ids["admin"])     # provenance: admin triggered

        report = (await db.execute(select(Report).filter(Report.id == run.report_id))).scalar_one()
        assert str(report.user_id) == str(ids["m_yes"])           # owned by target
        assert (report.artifact_visibility or "none") == "none"   # owner-private
        # completion ran as the target, not the admin
        assert any(c[1] == str(ids["m_yes"]) for c in calls)
        assert all(c[1] != str(ids["admin"]) for c in calls)
        print("[run-for] fan-out: 1 ran (private, owned by target), 1 skipped")


@pytest.mark.asyncio
async def test_run_for_group_principal(monkeypatch):
    _stub_completion(monkeypatch)
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])
        # group containing m_yes (eligible) and m_no (skipped)
        g = Group(organization_id=org.id, name=f"G {uuid.uuid4().hex[:6]}")
        db.add(g); await db.flush()
        db.add_all([
            GroupMembership(group_id=g.id, user_id=ids["m_yes"]),
            GroupMembership(group_id=g.id, user_id=ids["m_no"]),
        ])
        await db.commit()

        p = await prompt_service.create_prompt(
            db, PromptCreate(title="GroupRun", text="g", scope="agent",
                             data_source_ids=[ids["ds1"]]), admin, org)
        out = await prompt_service.run_prompt_for(
            db, p.id, admin, org, principal_type="group", group_id=g.id, parameters={})
        assert out["ran"] == 1 and out["skipped"] == 1
        print("[run-for] group principal expands to members")


@pytest.mark.asyncio
async def test_step0_admin_cannot_read_targets_run_for_report(monkeypatch):
    """Step-0 invariant: the actor (admin) must NOT be able to read the private
    report produced for the target. get_report's authorization lives in the
    @requires_permission(owner_only=True) decorator; here we assert the
    visibility-equivalent at the service layer: artifact_visibility=='none' and
    owner==target, and that ReportService._check_visibility('none') rejects a
    non-owner (the admin)."""
    _stub_completion(monkeypatch)
    ids = await _seed()
    async with async_session_maker() as db:
        org = await db.get(Organization, ids["org"])
        admin = await db.get(User, ids["admin"])

        p = await prompt_service.create_prompt(
            db, PromptCreate(title="Private", text="secret", scope="agent",
                             data_source_ids=[ids["ds1"]]), admin, org)
        out = await prompt_service.run_prompt_for(
            db, p.id, admin, org, principal_type="users", user_ids=[ids["m_yes"]], parameters={})
        assert out["ran"] == 1

        run = (await db.execute(select(PromptRun).filter(PromptRun.prompt_id == p.id))).scalars().one()
        report = (await db.execute(select(Report).filter(Report.id == run.report_id))).scalar_one()

        rs = ReportService()
        # Owner (target) can read.
        target = await db.get(User, ids["m_yes"])
        await rs._check_visibility(db, report, "artifact_visibility", user=target)

        # Admin (the actor) is a non-owner of a visibility='none' report → blocked.
        blocked = False
        try:
            await rs._check_visibility(db, report, "artifact_visibility", user=admin)
        except HTTPException as e:
            blocked = e.status_code in (403, 404)
        assert blocked, "admin must NOT be able to read the target's private run-for report"
        print("[step0] admin cannot read target's owner-private run-for report")
