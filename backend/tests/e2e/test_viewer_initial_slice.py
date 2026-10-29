"""A viewer's initial render shows the slice for the dashboard's DEFAULT values.

Viewers of a shared dashboard cache one result per parameter combination
(step_user_results). The initial render used to serve whichever combination
ran LAST — so after a member filtered to one region, their next visit showed
that region's rows while every host's filter control (which starts at the
declared defaults) read "All". The data and the control disagreed.

Contract: on the read paths that go through resolve_step_data (public step
view, batched views, exports), a non-owner gets their cached result for the
default values; other combinations stay cached but are not what the dashboard
opens on. With no default-values result of their own they get exactly what
they got before they ever ran anything (the shared snapshot, or nothing when
withheld). The owner is unaffected.
"""
import uuid

import pytest

from app.dependencies import async_session_maker
from app.models.membership import Membership
from app.models.report import Report
from app.models.user import User
from app.schemas.query_schema import QueryRunRequest
from app.services.query_service import QueryService
from app.services.report_service import ReportService
from tests.e2e.test_html_export_offline import _seed
from tests.e2e.test_run_query_params import _seed_query, _spec

pytestmark = pytest.mark.e2e

ALL_REGIONS = {"US", "DE", "FR"}


async def _member(report_id: str) -> User:
    sfx = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        user = User(name="Member", email=f"member-{sfx}@example.com", hashed_password="x",
                    is_active=True, is_superuser=False, is_verified=True)
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, organization_id=report.organization_id, role="member"))
        await db.commit()
        return user


async def _owner(report_id: str) -> User:
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        return await db.get(User, report.user_id)


async def _viewer_run(query_id: str, user: User, params: dict) -> dict:
    async with async_session_maker() as db:
        report_org = (await db.get(Report, (await _report_of(db, query_id)))).organization_id
        return await QueryService().run_query_viewer(
            db, query_id, QueryRunRequest(mode="viewer", params=params),
            organization_id=str(report_org), user_id=str(user.id),
        )


async def _report_of(db, query_id):
    from app.models.query import Query
    return (await db.get(Query, query_id)).report_id


async def _initial_regions(report_id: str, query_id: str, user) -> tuple[set, dict]:
    async with async_session_maker() as db:
        step = await ReportService().get_public_step(db, report_id, query_id, user=user)
    rows = (step.data or {}).get("rows") or []
    return {r["region"] for r in rows}, step.model_dump()


async def _report_with_region_query(visibility="public", extra_specs=()):
    report_id, _ = await _seed(artifact_visibility=visibility)
    # Snapshot (owner's view) answers region=US only.
    ids = await _seed_query(report_id, [_spec("region"), *extra_specs], snapshot_region="US")
    return report_id, ids["query_id"]


@pytest.mark.asyncio
async def test_initial_render_uses_the_default_slice_not_the_last_filtered_one():
    report_id, qid = await _report_with_region_query()
    member = await _member(report_id)

    default = await _viewer_run(qid, member, {})
    assert default["status"] == "success"
    filtered = await _viewer_run(qid, member, {"region": "DE"})  # ran LAST
    assert filtered["status"] == "success"

    regions, step = await _initial_regions(report_id, qid, member)
    assert regions == ALL_REGIONS, "the dashboard opens on its defaults (region: All)"
    assert step["viewer_result"]["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("filtered_region", ["DE", "FR"])
async def test_a_viewer_with_only_a_filtered_slice_sees_what_they_saw_before_running(filtered_region):
    report_id, qid = await _report_with_region_query()
    member = await _member(report_id)
    before, before_step = await _initial_regions(report_id, qid, member)

    await _viewer_run(qid, member, {"region": filtered_region})

    after, after_step = await _initial_regions(report_id, qid, member)
    assert after == before == {"US"}, "plain sharing: the shared snapshot, not the filtered run"
    assert after_step["viewer_result"] is None
    assert filtered_region not in after


@pytest.mark.asyncio
async def test_identity_scoped_dashboard_stays_withheld_until_the_default_run():
    """Withheld dashboards gate on 'no result of your own' to auto-run the
    defaults — a filtered slice must not count as that result."""
    identity = _spec("viewer_email", source="identity", identity_binding="viewer.email")
    report_id, qid = await _report_with_region_query(extra_specs=[identity])
    member = await _member(report_id)

    await _viewer_run(qid, member, {"region": "DE"})
    _, step = await _initial_regions(report_id, qid, member)
    assert step["snapshot_withheld"] is True and not (step["data"] or {}).get("rows")

    await _viewer_run(qid, member, {})
    regions, step = await _initial_regions(report_id, qid, member)
    assert step["snapshot_withheld"] is False
    assert regions == ALL_REGIONS


@pytest.mark.asyncio
async def test_owner_still_gets_the_shared_snapshot():
    report_id, qid = await _report_with_region_query()
    owner = await _owner(report_id)
    regions, step = await _initial_regions(report_id, qid, owner)
    assert regions == {"US"} and step["viewer_result"] is None
