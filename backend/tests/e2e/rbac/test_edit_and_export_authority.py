"""Side doors around edit/export authority (yearly security audit).

Each route below either persisted an edit or dumped authoring material while
skipping the gate its "front door" route enforces:

* ``POST /api/queries/{id}/run`` (builder mode) replaces the query's default
  step — the code every viewer executes — so only the query's owner may do it.
* ``POST /api/queries/steps/{id}/run`` rewrites a step's shared snapshot under
  the caller's credentials — owner of the report only.
* ``POST /api/entities/{id}/run`` with definition fields (code, title, status)
  is an edit: once an entity is approved its owner can no longer edit it via
  ``PUT``, so ``/run`` must not let them either.
* ``GET /api/evals/{name}.yaml`` dumps a suite's cases — ``manage_evals``,
  same as ``GET /api/tests/suites/{id}/export``.
* ``GET /api/agents/{name}.yaml`` dumps an agent's full manifest — the same
  ``manage`` bar as re-applying it.

Run:
    cd backend && uv run pytest tests/e2e/rbac/test_edit_and_export_authority.py -v
"""
import uuid

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.widget import Widget


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


async def _seed_query_for(user_id, org_id):
    """Report + widget + query + default step owned by ``user_id``.

    Direct DB writes (tests/AGENTS.md rule 5): a Step with generated code is
    produced by the agent calling an LLM, which an e2e test cannot do.
    """
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = Report(title=f"R {suffix}", slug=f"r-{suffix}", status="draft",
                        user_id=user_id, organization_id=org_id)
        db.add(report)
        await db.flush()
        widget = Widget(title="W", slug=f"w-{suffix}", report_id=report.id)
        db.add(widget)
        await db.flush()
        query = Query(title="Q", report_id=report.id, widget_id=widget.id,
                      organization_id=org_id, user_id=user_id)
        db.add(query)
        await db.flush()
        step = Step(title="S", slug=f"s-{suffix}", status="success",
                    widget_id=widget.id, query_id=query.id, code="SELECT 1",
                    data={"rows": [{"owner_row": 1}], "columns": [{"field": "owner_row"}]},
                    data_model={"type": "table"}, view={"type": "table"})
        db.add(step)
        await db.flush()
        query.default_step_id = step.id
        await db.commit()
        return {"query_id": str(query.id), "step_id": str(step.id), "report_id": str(report.id)}


async def _default_step(query_id):
    async with async_session_maker() as db:
        return (await db.execute(
            select(Query.default_step_id).where(Query.id == query_id)
        )).scalar_one()


async def _step_data(step_id):
    async with async_session_maker() as db:
        return (await db.execute(select(Step.data).where(Step.id == step_id))).scalar_one()


BUILDER_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    return pd.DataFrame({'injected': [1]})\n"
)


# ── Queries ───────────────────────────────────────────────────────────────


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("owner_role", ["admin", "member"])
async def test_only_the_query_owner_can_replace_its_code(
    test_client, bootstrap_admin, invite_user_to_org, owner_role,
):
    """Both directions: a member can't rewrite an admin's query, and an admin
    (mutations stay owner-only) can't rewrite a member's."""
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    owner, other = (admin, member) if owner_role == "admin" else (member, admin)
    seeded = await _seed_query_for(owner["user_id"], org_id)
    before = await _default_step(seeded["query_id"])

    resp = test_client.post(
        f"/api/queries/{seeded['query_id']}/run",
        json={"code": BUILDER_CODE, "mode": "builder"},
        headers=_hdr(other["token"], org_id),
    )
    assert resp.status_code == 403, resp.text
    assert await _default_step(seeded["query_id"]) == before

    own = test_client.post(
        f"/api/queries/{seeded['query_id']}/run",
        json={"code": BUILDER_CODE, "mode": "builder"},
        headers=_hdr(owner["token"], org_id),
    )
    assert own.status_code != 403, own.text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_viewer_mode_run_stays_open_to_non_owners(
    test_client, bootstrap_admin, invite_user_to_org,
):
    """Viewer mode stays open to non-owners who can read the report (here: a
    published dashboard). A private report's queries are not theirs to run."""
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    seeded = await _seed_query_for(admin["user_id"], admin["org_id"])

    def run():
        return test_client.post(
            f"/api/queries/{seeded['query_id']}/run",
            json={"mode": "viewer", "params": {}},
            headers=_hdr(member["token"], admin["org_id"]),
        )

    assert run().status_code == 404
    shared = test_client.put(
        f"/api/reports/{seeded['report_id']}/visibility/artifact",
        json={"visibility": "internal"}, headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert shared.status_code == 200, shared.text
    resp = run()
    assert resp.status_code not in (403, 404), resp.text


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_only_the_report_owner_can_rerun_a_step_into_its_snapshot(
    test_client, bootstrap_admin, invite_user_to_org,
):
    admin = bootstrap_admin()
    member = invite_user_to_org(org_id=admin["org_id"], admin_token=admin["token"])
    seeded = await _seed_query_for(admin["user_id"], admin["org_id"])
    before = await _step_data(seeded["step_id"])

    resp = test_client.post(
        f"/api/queries/steps/{seeded['step_id']}/run",
        headers=_hdr(member["token"], admin["org_id"]),
    )
    assert resp.status_code == 403, resp.text
    assert await _step_data(seeded["step_id"]) == before

    missing = test_client.post(
        f"/api/queries/steps/{uuid.uuid4()}/run",
        headers=_hdr(admin["token"], admin["org_id"]),
    )
    assert missing.status_code == 404, missing.text


# ── Entities ──────────────────────────────────────────────────────────────

ENTITY_CODE = (
    "def generate_df(ds_clients, excel_files):\n"
    "    import pandas as pd\n"
    "    return pd.DataFrame({'v': [1]})\n"
)


@pytest.fixture
def approved_member_entity(test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source):
    """An entity a plain member authored, suggested, and an admin approved."""
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    ds = sqlite_data_source(name=f"ent_{uuid.uuid4().hex[:6]}", user_token=admin["token"],
                            org_id=org_id, is_public=True)
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    mh, ah = _hdr(member["token"], org_id), _hdr(admin["token"], org_id)

    created = test_client.post("/api/entities", json={
        "type": "model", "title": "Original", "slug": f"e-{uuid.uuid4().hex[:8]}",
        "code": ENTITY_CODE, "data": {}, "status": "published",
        "data_source_ids": [ds["id"]],
    }, headers=mh)
    assert created.status_code == 200, created.text
    eid = created.json()["id"]
    assert test_client.post(f"/api/entities/{eid}/suggest", headers=mh).status_code == 200
    approved = test_client.post(f"/api/entities/{eid}/approve", headers=ah)
    assert approved.status_code == 200, approved.text
    assert approved.json()["global_status"] == "approved"
    return {"id": eid, "member_h": mh, "admin_h": ah}


@pytest.mark.e2e
@pytest.mark.parametrize("field,value", [
    ("code", "def generate_df(ds_clients, excel_files):\n    import pandas as pd\n    return pd.DataFrame({'evil': [1]})\n"),
    ("title", "Renamed by owner"),
    ("status", "archived"),
])
def test_owner_cannot_edit_an_approved_entity_through_run(
    test_client, approved_member_entity, field, value,
):
    e = approved_member_entity
    # The front door is closed for this owner …
    put = test_client.put(f"/api/entities/{e['id']}", json={field: value}, headers=e["member_h"])
    assert put.status_code == 403, put.text
    # … so the side door must be too.
    run = test_client.post(f"/api/entities/{e['id']}/run", json={field: value}, headers=e["member_h"])
    assert run.status_code == 403, run.text
    after = test_client.get(f"/api/entities/{e['id']}", headers=e["admin_h"]).json()
    assert after[field] != value


@pytest.mark.e2e
def test_approved_entity_stays_refreshable_and_editable_by_authority(
    test_client, approved_member_entity,
):
    e = approved_member_entity
    # A plain refresh (no definition change) by the owner is still allowed.
    assert test_client.post(f"/api/entities/{e['id']}/run", json={}, headers=e["member_h"]).status_code == 200
    # An admin may still edit through run.
    r = test_client.post(f"/api/entities/{e['id']}/run", json={"title": "Admin edit"}, headers=e["admin_h"])
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Admin edit"


# ── YAML exports ──────────────────────────────────────────────────────────


@pytest.mark.e2e
def test_eval_yaml_export_requires_manage_evals(test_client, bootstrap_admin, invite_user_to_org):
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    name = f"suite-{uuid.uuid4().hex[:8]}"
    created = test_client.post("/api/tests/suites", json={"name": name},
                               headers=_hdr(admin["token"], org_id))
    assert created.status_code == 200, created.text

    denied = test_client.get(f"/api/evals/{name}.yaml", headers=_hdr(member["token"], org_id))
    assert denied.status_code == 403, denied.text
    assert name not in denied.text

    ok = test_client.get(f"/api/evals/{name}.yaml", headers=_hdr(admin["token"], org_id))
    assert ok.status_code == 200, ok.text
    assert name in ok.text


@pytest.mark.e2e
def test_agent_yaml_export_requires_manage_on_the_agent(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource,
):
    admin = bootstrap_admin()
    org_id = admin["org_id"]
    ds = sqlite_data_source(name=f"agent_{uuid.uuid4().hex[:6]}", user_token=admin["token"],
                            org_id=org_id, is_public=True)
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    manager = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    grant_resource(resource_type="data_source", resource_id=ds["id"], principal_type="user",
                   principal_id=manager["user_id"], permissions=["manage"],
                   user_token=admin["token"], org_id=org_id)
    url = f"/api/agents/{ds['name']}.yaml"

    # A member who can USE the agent still may not dump its configuration.
    denied = test_client.get(url, headers=_hdr(member["token"], org_id))
    assert denied.status_code == 403, denied.text

    for who in (admin, manager):
        ok = test_client.get(url, headers=_hdr(who["token"], org_id))
        assert ok.status_code == 200, ok.text
        assert ds["name"] in ok.text


@pytest.mark.e2e
@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["suggest", "withdraw"])
async def test_entity_workflow_denials_conceal_source_restricted_entities(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, action,
):
    from app.models.entity import Entity

    admin = bootstrap_admin()
    org_id = admin["org_id"]
    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    owner_headers = _hdr(admin["token"], org_id)
    member_headers = _hdr(member["token"], org_id)
    created = test_client.post("/api/entities", json={
        "type": "model", "title": "Restricted snapshot",
        "slug": f"snapshot-{uuid.uuid4().hex[:8]}",
        "code": ENTITY_CODE, "data": {}, "status": "published",
    }, headers=owner_headers)
    assert created.status_code == 200, created.text
    entity_id = created.json()["id"]
    # Source lineage is server-generated by monitoring/LLM execution; it cannot
    # be supplied through entity authoring APIs. Seed that protected state only.
    async with async_session_maker() as db:
        entity = await db.get(Entity, entity_id)
        entity.bow_source_access = {"organization_id": org_id, "scope_ids": None, "report_ids": []}
        await db.commit()
    hidden = test_client.post(f"/api/entities/{entity_id}/{action}", headers=member_headers)
    missing = test_client.post(f"/api/entities/{uuid.uuid4()}/{action}", headers=member_headers)
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    public_agent = sqlite_data_source(
        name=f"workflow-{uuid.uuid4().hex[:8]}", user_token=admin["token"],
        org_id=org_id, is_public=True,
    )
    owned = test_client.post("/api/entities", json={
        "type": "model", "title": "Member-owned suggestion",
        "slug": f"owned-{uuid.uuid4().hex[:8]}",
        "code": ENTITY_CODE, "data": {}, "status": "published",
        "data_source_ids": [public_agent["id"]],
    }, headers=member_headers)
    assert owned.status_code == 200, owned.text
    owned_id = owned.json()["id"]
    withdrawn = test_client.post(f"/api/entities/{owned_id}/withdraw", headers=member_headers)
    assert withdrawn.status_code == 200, withdrawn.text
    suggested = test_client.post(f"/api/entities/{owned_id}/suggest", headers=member_headers)
    assert suggested.status_code == 200, suggested.text
