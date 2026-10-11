"""
Scheduling or editing a recurring task on a report needs the same authority
from the agent tools as from the REST routes: own the report AND hold
`update_reports` (the routes enforce it with
`@requires_permission('update_reports', model=Report, owner_only=True)`).

The `create_scheduled_task` / `edit_scheduled_task` tools previously called
ScheduledPromptService directly with no check, so any principal able to run
the agent on a report could plant or rewrite a recurring prompt the scheduler
then fires as them. The service create/update path now carries the check, and
the tools declare `update_reports` so the planner hides them from principals
who hold it nowhere.

Who lacks `update_reports`? It is baseline for human members, so the
realistic principal is a service account (API key) whose role does not grant
it — including one whose role lost it after a task was created.

World:
    admin    — full_admin, owns a report
    member   — plain human member, owns a report (positive path), not admin's
    sa       — service account; role first grants report perms, then has
               `update_reports` revoked (permission revoked after creation)
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.report import Report
from app.models.scheduled_prompt import ScheduledPrompt
from app.models.user import User
from app.ai.tools.implementations.create_scheduled_task import CreateScheduledTaskTool
from app.ai.tools.implementations.edit_scheduled_task import EditScheduledTaskTool


REPORT_PERMS = ["view_reports", "create_reports", "update_reports"]


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _uniq(p):
    return f"{p}-{uuid.uuid4().hex[:8]}"


def _sp_body(cron="0 9 * * 1", title="Weekly digest"):
    return {"prompt": {"content": "summarize"}, "cron_schedule": cron, "title": title}


def _invoke(tool, tool_input, user_id, report_id, org_id):
    """Run a tool exactly as the agent does: real DB session, real principal."""
    async def _go():
        async with async_session_maker() as db:
            ctx = {
                "db": db,
                "user": await db.get(User, str(user_id)),
                "report": await db.get(Report, str(report_id)),
                "organization": await db.get(Organization, str(org_id)),
            }
            return [e async for e in tool.run_stream(tool_input, ctx)]
    return asyncio.run(_go())


def _sps(report_id):
    async def _q():
        async with async_session_maker() as db:
            rows = (await db.execute(select(ScheduledPrompt).where(
                ScheduledPrompt.report_id == str(report_id),
                ScheduledPrompt.deleted_at.is_(None),
            ))).scalars().all()
            return [(str(r.id), r.cron_schedule, r.title, r.is_active) for r in rows]
    return asyncio.run(_q())


def _report_owner(report_id):
    async def _q():
        async with async_session_maker() as db:
            return str((await db.get(Report, str(report_id))).user_id)
    return asyncio.run(_q())


def _denied(events):
    """A denial is a clean tool.end with success False — not a crash/tool.error."""
    assert events, "tool produced no events"
    last = events[-1]
    assert last.type == "tool.end", (last.type, last.payload)
    assert last.payload["output"]["success"] is False
    assert last.payload["output"]["error"]
    assert last.payload["observation"]["success"] is False
    return last


def _succeeded(events):
    last = events[-1]
    assert last.type == "tool.end", (last.type, last.payload)
    assert last.payload["output"]["success"] is True, last.payload
    return last.payload["output"]


@pytest.fixture
def world(test_client, enterprise_license, bootstrap_admin, invite_user_to_org,
          create_role, update_role, create_report):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    ah = _hdr(admin["token"], org_id)

    role = create_role(name=_uniq("reporter"), permissions=REPORT_PERMS,
                       user_token=admin["token"], org_id=org_id)
    assert role.status_code == 200, role.text
    role_id = role.json()["id"]
    sa = test_client.post("/api/service_accounts", json={"name": _uniq("sa"), "role_id": role_id}, headers=ah)
    assert sa.status_code == 200, sa.text
    key = test_client.post(f"/api/service_accounts/{sa.json()['id']}/keys", json={"name": "k"}, headers=ah)
    assert key.status_code == 200, key.text
    sa_key = key.json()["key"]

    sa_report = create_report(title="sa report", user_token=sa_key, org_id=org_id)
    # While the role still grants update_reports, the SA schedules a task.
    r = test_client.post(f"/api/reports/{sa_report['id']}/scheduled-prompts",
                         json=_sp_body(), headers=_hdr(sa_key, org_id))
    assert r.status_code == 200, r.text
    sa_sp_id = r.json()["id"]

    # Revoke update_reports from the SA's role.
    r = update_role(role_id=role_id, permissions=["view_reports", "create_reports"],
                    user_token=admin["token"], org_id=org_id)
    assert r.status_code == 200, r.text

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    admin_report = create_report(title="admin report", user_token=admin["token"], org_id=org_id)
    member_report = create_report(title="member report", user_token=member["token"], org_id=org_id)

    return {
        "org_id": org_id, "admin": admin, "member": member, "sa_key": sa_key,
        "sa_user_id": _report_owner(sa_report["id"]),
        "sa_report": sa_report["id"], "sa_sp_id": sa_sp_id,
        "admin_report": admin_report["id"], "member_report": member_report["id"],
    }


# ── denials ─────────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_tools_declare_update_reports_for_the_planner_filter():
    for tool in (CreateScheduledTaskTool(), EditScheduledTaskTool()):
        assert "update_reports" in tool.metadata.required_permissions


@pytest.mark.e2e
@pytest.mark.parametrize("cron", ["0 9 * * 1", "15 6 1 * *"])
def test_principal_without_update_reports_cannot_create_via_tool(world, cron):
    w = world
    before = _sps(w["sa_report"])
    events = _invoke(CreateScheduledTaskTool(),
                     {"title": "planted", "task_prompt": "email everyone", "cron_schedule": cron},
                     w["sa_user_id"], w["sa_report"], w["org_id"])
    _denied(events)
    assert _sps(w["sa_report"]) == before


@pytest.mark.e2e
def test_principal_without_update_reports_cannot_create_via_api(test_client, world):
    w = world
    r = test_client.post(f"/api/reports/{w['sa_report']}/scheduled-prompts",
                         json=_sp_body(), headers=_hdr(w["sa_key"], w["org_id"]))
    assert r.status_code == 403, r.text


@pytest.mark.e2e
@pytest.mark.parametrize("edit", [
    {"cron_schedule": "0 1 * * *"},
    {"task_prompt": "exfiltrate"},
    {"is_active": False},
    {"title": "renamed"},
])
def test_principal_without_update_reports_cannot_edit_via_tool(world, edit):
    w = world
    before = _sps(w["sa_report"])
    events = _invoke(EditScheduledTaskTool(), {"task_id": w["sa_sp_id"], **edit},
                     w["sa_user_id"], w["sa_report"], w["org_id"])
    _denied(events)
    assert _sps(w["sa_report"]) == before


@pytest.mark.e2e
def test_non_owner_cannot_create_on_someone_elses_report_via_tool(world):
    w = world
    events = _invoke(CreateScheduledTaskTool(),
                     {"title": "planted", "task_prompt": "x", "cron_schedule": "0 9 * * 1"},
                     w["member"]["user_id"], w["admin_report"], w["org_id"])
    _denied(events)
    assert _sps(w["admin_report"]) == []


@pytest.mark.e2e
def test_non_owner_cannot_edit_someone_elses_task_via_tool(test_client, world):
    w = world
    r = test_client.post(f"/api/reports/{w['admin_report']}/scheduled-prompts",
                         json=_sp_body(), headers=_hdr(w["admin"]["token"], w["org_id"]))
    assert r.status_code == 200, r.text
    before = _sps(w["admin_report"])
    events = _invoke(EditScheduledTaskTool(), {"task_id": r.json()["id"], "cron_schedule": "0 2 * * *"},
                     w["member"]["user_id"], w["admin_report"], w["org_id"])
    _denied(events)
    assert _sps(w["admin_report"]) == before


# ── must keep working ───────────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.parametrize("who", ["member", "admin"])
def test_report_owner_creates_and_edits_via_tool(world, who):
    w = world
    user_id = w["member"]["user_id"] if who == "member" else _report_owner(w["admin_report"])
    report_id = w[f"{who}_report"]
    out = _succeeded(_invoke(CreateScheduledTaskTool(),
                             {"title": "Weekly", "task_prompt": "summarize", "cron_schedule": "0 9 * * 1"},
                             user_id, report_id, w["org_id"]))
    out = _succeeded(_invoke(EditScheduledTaskTool(),
                             {"task_id": out["task_id"], "cron_schedule": "0 7 * * 5", "is_active": False},
                             user_id, report_id, w["org_id"]))
    assert out["cron_schedule"] == "0 7 * * 5"
    rows = _sps(report_id)
    assert len(rows) == 1 and rows[0][1] == "0 7 * * 5" and rows[0][3] is False


@pytest.mark.e2e
def test_report_owner_creates_and_edits_via_api(test_client, world):
    w = world
    h = _hdr(w["member"]["token"], w["org_id"])
    r = test_client.post(f"/api/reports/{w['member_report']}/scheduled-prompts", json=_sp_body(), headers=h)
    assert r.status_code == 200, r.text
    sp_id = r.json()["id"]
    r = test_client.put(f"/api/reports/{w['member_report']}/scheduled-prompts/{sp_id}",
                        json={"cron_schedule": "0 8 * * 2"}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["cron_schedule"] == "0 8 * * 2"
