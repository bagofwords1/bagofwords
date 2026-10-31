"""Agent List writes from generated code (Sandbox setting "Write to Agent Lists from code").

Contract:
- the code may write only to the lists the acting user could submit to with
  submit_<list>: an agent attached to the report and MANAGE on it (view-only
  users get no list in the helper at all);
- rows are saved by key after the run succeeds, the last submission of a
  key wins, and only counts come back;
- access is checked again at save time: a list deleted mid-run saves nothing;
- a quote is verified against the pages the code fetched in that run;
- every Sandbox setting is off for a new organization.

Run: cd backend && uv run pytest tests/e2e/rbac/test_sandbox_list_writes.py -q --db=sqlite
"""
from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.e2e


def _h(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _schema(require_evidence=False):
    return {"name": "Companies", "description": "Scraped companies", "key_field": "company",
            "require_evidence": require_evidence, "fields": [
                {"name": "company", "type": "string", "required": True, "method": "extract"},
                {"name": "employees", "type": "integer"},
                {"name": "city", "type": "string"},
            ]}


def _db_url():
    return (os.environ["TEST_DATABASE_URL"]
            .replace("sqlite://", "sqlite+aiosqlite://", 1)
            .replace("postgresql://", "postgresql+asyncpg://", 1))


def run_async(fn):
    async def _run():
        engine = create_async_engine(_db_url())
        try:
            maker = async_sessionmaker(engine, expire_on_commit=False)
            async with maker() as db:
                return await fn(db)
        finally:
            await engine.dispose()
    return asyncio.run(_run())


@pytest.fixture
def world(bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource, create_report, test_client):
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]
    agent = sqlite_data_source(name=f"Research {uuid.uuid4().hex[:4]}", user_token=admin["token"], org_id=org_id)
    viewer = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    for who, perms in ((viewer, ["view"]), (manager, ["manage"])):
        r = grant_resource(resource_type="data_source", resource_id=agent["id"], principal_type="user",
                           principal_id=who["user_id"], permissions=perms, user_token=admin["token"], org_id=org_id)
        assert r.status_code in (200, 201), r.text
    report = create_report(title="Scrape", user_token=admin["token"], org_id=org_id, data_sources=[agent["id"]])
    return {"org_id": org_id, "admin": admin, "viewer": viewer, "manager": manager,
            "agent": agent, "report": report, "client": test_client}


def _make_list(world, **kw):
    r = world["client"].post(f"/api/data_sources/{world['agent']['id']}/lists", json=_schema(**kw),
                             headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 201, r.text
    return r.json()


def _rows(world, lst):
    r = world["client"].get(f"/api/data_sources/{world['agent']['id']}/lists/{lst['id']}/rows",
                            headers=_h(world["admin"]["token"], world["org_id"]))
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def _run_code(world, actor, submit_calls, *, before_commit=None, pages=(), platform_context=None):
    """What create_data does around a successful run: load the lists this
    user may write to, let the code submit, then save."""
    from app.ai.code_execution.sandbox_helpers import PageTextStore
    from app.models.organization import Organization
    from app.models.report import Report
    from app.models.user import User
    from app.services.agent_lists.sandbox_sink import (
        SandboxListSink, commit_sandbox_submissions, load_sandbox_lists,
    )

    async def _fn(db):
        report = await db.get(Report, world["report"]["id"])
        user = await db.get(User, world[actor]["user_id"])
        org = await db.get(Organization, world["org_id"])
        lists = await load_sandbox_lists(db, report, user, org, platform_context=platform_context)
        sink = SandboxListSink(lists=lists, max_rows=1000)
        outs = [sink.submit(name, recs) for name, recs in submit_calls]
        if before_commit:
            before_commit()
        texts = PageTextStore()
        for url, text in pages:
            texts.add(url, text)
        saved = await commit_sandbox_submissions(
            db, sink.take(), report=report, user=user, organization=org,
            tool_execution_id=str(uuid.uuid4()), extra_sources=texts.to_sources(),
            platform_context=platform_context,
        )
        return [l.slug for l in lists], outs, saved

    return run_async(_fn)


def _field(row, lst, name):
    fid = next(f["id"] for f in lst["fields"] if f["name"] == name)
    return row["values"][fid]


def test_rows_from_code_are_saved_by_key_and_only_counts_come_back(world):
    lst = _make_list(world)
    slugs, outs, saved = _run_code(world, "admin", [
        ("companies", [{"company": "Acme", "employees": 10}, {"company": "Globex", "employees": 5}]),
        # Seen again on a later page: the last value wins, no duplicate row.
        ("Companies", [{"company": "acme", "employees": 12, "city": "Haifa"}]),
    ])
    assert slugs == [lst["slug"]]
    assert all(set(o) == {"list", "queued", "total_queued"} for o in outs)
    assert saved["inserted"] == 2 and saved["rejected"] == 0 and not saved["errors"]

    rows = {r["key_value"]: r for r in _rows(world, lst)}
    assert set(rows) == {"acme", "globex"}
    assert _field(rows["acme"], lst, "employees")["value"] == 12
    assert _field(rows["acme"], lst, "city")["value"] == "Haifa"

    # A second run updates in place.
    _, _, again = _run_code(world, "admin", [("companies", [{"company": "Globex", "employees": 6}])])
    assert again["updated"] == 1 and again["inserted"] == 0
    assert len(_rows(world, lst)) == 2


@pytest.mark.parametrize("actor, can_write", [("manager", True), ("viewer", False)])
def test_only_users_who_may_submit_get_the_list(world, actor, can_write):
    lst = _make_list(world)
    if can_write:
        slugs, _, saved = _run_code(world, actor, [("companies", [{"company": "Initech"}])])
        assert slugs == [lst["slug"]] and saved["inserted"] == 1
        assert [r["key_value"] for r in _rows(world, lst)] == ["initech"]
    else:
        from app.services.agent_lists.sandbox_sink import SandboxListError
        with pytest.raises(SandboxListError, match="Available lists: none"):
            _run_code(world, actor, [("companies", [{"company": "Initech"}])])
        assert _rows(world, lst) == []


def test_a_list_deleted_before_the_save_gets_nothing(world):
    lst = _make_list(world)

    def _delete():
        r = world["client"].delete(f"/api/data_sources/{world['agent']['id']}/lists/{lst['id']}",
                                   headers=_h(world["admin"]["token"], world["org_id"]))
        assert r.status_code == 204, r.text

    _, _, saved = _run_code(world, "admin", [("companies", [{"company": "Acme"}, {"company": "Hooli"}])],
                            before_commit=_delete)
    assert saved["inserted"] == 0 and saved["rejected"] == 2 and saved["errors"]


def test_quotes_are_verified_against_pages_the_code_fetched(world):
    lst = _make_list(world, require_evidence=True)
    url = "https://example.com/companies/acme"
    page = "Acme Corporation employs 120 people in its Haifa office."
    _, _, saved = _run_code(world, "admin", [("companies", [
        {"company": {"value": "Acme", "status": "found",
                     "evidence": [{"quote": "Acme Corporation employs 120 people", "ref": url}]}},
        {"company": {"value": "Globex", "status": "found",
                     "evidence": [{"quote": "Globex is the largest employer", "ref": url}]}},
    ])], pages=[(url, page)])
    assert saved["inserted"] == 2
    rows = {r["key_value"]: r for r in _rows(world, lst)}
    acme_ev = _field(rows["acme"], lst, "company")["evidence"][0]
    globex_ev = _field(rows["globex"], lst, "company")["evidence"][0]
    assert acme_ev["kind"] == "web" and acme_ev["ref"] == url
    assert acme_ev["verified"] is True
    assert globex_ev["verified"] is False  # not on any fetched page


SANDBOX_KEYS = ("enable_sandbox_web_fetch", "enable_sandbox_browser",
                "sandbox_browser_allow_mutating_requests", "enable_sandbox_lists")


def test_sandbox_settings_are_off_for_a_new_org_and_admins_can_turn_them_on(world):
    h = _h(world["admin"]["token"], world["org_id"])
    r = world["client"].get("/api/organization/settings", headers=h)
    assert r.status_code == 200, r.text
    config = r.json()["config"]
    for key in SANDBOX_KEYS:
        assert config[key]["value"] is False, key

    r = world["client"].put("/api/organization/settings", headers=h,
                            json={"config": {"enable_sandbox_lists": {"value": True}}})
    assert r.status_code == 200, r.text
    assert r.json()["config"]["enable_sandbox_lists"]["value"] is True

    # A member without manage_settings cannot.
    r = world["client"].put("/api/organization/settings",
                            headers=_h(world["viewer"]["token"], world["org_id"]),
                            json={"config": {"enable_sandbox_browser": {"value": True}}})
    assert r.status_code == 403


@pytest.mark.parametrize("surface,allowed", [("app", True), ("bubble", False)])
def test_sandbox_lists_follow_the_conversation_surface(world, surface, allowed):
    from sqlalchemy import update
    from app.models.report import Report
    lst = _make_list(world)

    # Runtime-only hidden report type, unavailable in the public report-create schema.
    async def mark_thread(db):
        await db.execute(update(Report).where(Report.id == world["report"]["id"])
                         .values(report_type="artifact_chat"))
        await db.commit()
    run_async(mark_thread)
    calls = [(lst["slug"], [{"company": "Acme", "employees": 42}])]
    if allowed:
        slugs, _, saved = _run_code(world, "admin", calls, platform_context={"surface": surface})
        assert slugs == [lst["slug"]]
        assert saved["inserted"] == 1
    else:
        from app.services.agent_lists.sandbox_sink import SandboxListError
        with pytest.raises(SandboxListError):
            _run_code(world, "admin", calls, platform_context={"surface": surface})
        assert _rows(world, lst) == []
