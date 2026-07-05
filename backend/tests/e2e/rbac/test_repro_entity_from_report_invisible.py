"""
REPRODUCTION (do not fix here): "User creates a new entity from the report
page but can't see it in the queries page."

Two independent mechanisms are demonstrated:

1. MEMBER (non-admin) create-from-step yields a *suggested* entity, never a
   *global* one. The /queries page defaults to the "Published" tab which only
   renders global (approved+published) entities, so a freshly created entity
   is invisible there and only shows under the "My Drafts/Suggested" tab.
   (Backend returns it, but with private_status/global_status that the default
   tab filters out.)

2. create_entity_from_step copies ALL of the source report's data sources onto
   the new entity. list_entities hides any entity that has *any* data source
   the caller cannot access (``~has_inaccessible_ds``). So if the report spans
   a data source the creator has no grant on, the creator cannot see their own
   just-created entity AT ALL — matching the fully-empty queries screenshot.

These tests are expected to expose the bug (assertions describe the buggy
observed behavior with explanatory messages).
"""
import uuid
import asyncio

import pytest

from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.models.entity import entity_data_source_association
from app.models.report_data_source_association import report_data_source_association


def _hdr(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _seed_report_with_step(*, org_id, owner_id, ds_ids):
    """Insert Report -> Widget -> Query -> successful Step directly in the DB
    and attach the given data sources to the report. Returns the step id."""
    from app.settings.database import create_async_session_factory

    async def _run():
        factory = create_async_session_factory()
        async with factory() as db:
            suffix = uuid.uuid4().hex[:8]
            report = Report(
                title="Repro Report",
                slug=f"repro-report-{suffix}",
                status="active",
                user_id=str(owner_id),
                organization_id=str(org_id),
            )
            db.add(report)
            await db.flush()

            for ds_id in ds_ids:
                await db.execute(
                    report_data_source_association.insert().values(
                        report_id=str(report.id), data_source_id=str(ds_id)
                    )
                )

            widget = Widget(
                title="Repro Widget",
                slug=f"repro-widget-{suffix}",
                status="published",
                report_id=str(report.id),
            )
            db.add(widget)
            await db.flush()

            query = Query(
                title="Repro Query",
                widget_id=str(widget.id),
                report_id=str(report.id),
                organization_id=str(org_id),
                user_id=str(owner_id),
            )
            db.add(query)
            await db.flush()

            step = Step(
                title="Repro Step",
                slug=f"repro-step-{suffix}",
                status="success",
                code="select 1 as value",
                data={"rows": [{"value": 1}], "info": {"total_rows": 1, "total_columns": 1}},
                data_model={},
                view={"type": "table"},
                widget_id=str(widget.id),
                query_id=str(query.id),
            )
            db.add(step)
            await db.flush()
            await db.commit()
            return str(step.id)

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


@pytest.mark.e2e
def test_member_from_step_entity_hidden_on_default_published_tab(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource
):
    """Mechanism #1: member's from_step entity is 'suggested', which the
    default 'Published' tab of /queries filters out."""
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    ds = sqlite_data_source(name="repro_ds_public", user_token=admin["token"], org_id=org_id, is_public=True)

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    # Member needs a per-DS create_entities grant to reach from_step at all.
    grant_resource(
        resource_type="data_source", resource_id=ds["id"],
        principal_type="user", principal_id=member["user_id"],
        permissions=["create_entities"],
        user_token=admin["token"], org_id=org_id,
    )

    step_id = _seed_report_with_step(org_id=org_id, owner_id=member["user_id"], ds_ids=[ds["id"]])

    create = test_client.post(
        f"/api/entities/from_step/{step_id}",
        json={"type": "model", "title": "Yossi Rost June", "description": None,
              "publish": False, "data_source_ids": []},
        headers=_hdr(member["token"], org_id),
    )
    assert create.status_code == 200, create.text
    ent = create.json()

    # Backend DOES return it in the raw list...
    listed = test_client.get("/api/entities", headers=_hdr(member["token"], org_id))
    assert listed.status_code == 200, listed.text
    ids = {e["id"] for e in listed.json()}
    assert ent["id"] in ids, "entity missing from raw list entirely"

    # ...but its status combo makes it 'suggested', NOT 'global'. The default
    # /queries "Published" tab renders only global (approved+published).
    print("MEMBER ENTITY STATUS:", ent.get("private_status"), ent.get("global_status"), ent.get("status"))
    is_global = (not ent.get("private_status")) and ent.get("global_status") == "approved" and ent.get("status") == "published"
    assert not is_global, "unexpected: member entity is global (would show on default tab)"
    # This is the bug: user lands on default 'Published' tab and sees nothing.


@pytest.mark.e2e
def test_from_step_entity_fully_hidden_when_report_has_inaccessible_ds(
    test_client, bootstrap_admin, invite_user_to_org, sqlite_data_source, grant_resource
):
    """Mechanism #2: from_step copies ALL report data sources; if one is
    inaccessible to the creator, list_entities hides the entity entirely —
    the fully-empty queries page from the screenshot."""
    admin = bootstrap_admin("admin")
    org_id = admin["org_id"]

    ds_a = sqlite_data_source(name="repro_ds_a", user_token=admin["token"], org_id=org_id, is_public=False)
    ds_b = sqlite_data_source(name="repro_ds_b", user_token=admin["token"], org_id=org_id, is_public=False)

    member = invite_user_to_org(org_id=org_id, admin_token=admin["token"])
    # Member can create entities on DS A only. DS B remains inaccessible.
    grant_resource(
        resource_type="data_source", resource_id=ds_a["id"],
        principal_type="user", principal_id=member["user_id"],
        permissions=["create_entities"],
        user_token=admin["token"], org_id=org_id,
    )

    # Report spans BOTH data sources.
    step_id = _seed_report_with_step(
        org_id=org_id, owner_id=member["user_id"], ds_ids=[ds_a["id"], ds_b["id"]]
    )

    create = test_client.post(
        f"/api/entities/from_step/{step_id}",
        json={"type": "model", "title": "Cross-DS entity", "description": None,
              "publish": False, "data_source_ids": []},
        headers=_hdr(member["token"], org_id),
    )
    assert create.status_code == 200, create.text
    ent = create.json()
    print("CREATED ENTITY DS COUNT:", len(ent.get("data_sources") or []))

    listed = test_client.get("/api/entities", headers=_hdr(member["token"], org_id))
    assert listed.status_code == 200, listed.text
    ids = {e["id"] for e in listed.json()}

    # BUG: the creator cannot see their own just-created entity at all.
    assert ent["id"] not in ids, (
        "expected the entity to be hidden (repro), but it appeared in the list"
    )
