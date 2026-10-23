"""GET /api/r/{id}/queries_with_steps serves exactly what the per-step view does.

The shared page used to load a dashboard's data in two round trips: the query
list (GET /r/{id}/queries), then one GET /r/{id}/queries/{qid}/step per query.
queries_with_steps returns both in one response. It is a public, optionally
anonymous endpoint that serves step rows, so the contract is strict parity:
for EVERY reader — anonymous, the owner, an org member with or without a run
of their own, an outsider — and every query, `default_step` must equal what
the per-step route returns that same reader (rows, withheld flag, viewer
result, code redaction, applied_params redaction), and the query fields must
equal the list route's. Visibility gating must match the list route too.
"""
import uuid
from datetime import datetime

import pytest
from fastapi import HTTPException

from app.dependencies import async_session_maker
from app.models.membership import Membership
from app.models.organization import Organization
from app.models.report import Report
from app.models.step_user_result import StepUserResult
from app.models.user import User
from app.services.report_service import ReportService
from tests.e2e.test_html_export_offline import _seed
from tests.e2e.test_public_output_parity import IDENTITY_PARAMS, _add_plain_query

pytestmark = pytest.mark.e2e


async def _readers(report_id: str) -> dict:
    """owner, a member of the report's org, and a user from another org."""
    sfx = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        owner = await db.get(User, report.user_id)
        member = User(name="Member", email=f"member-{sfx}@example.com", hashed_password="x",
                      is_active=True, is_superuser=False, is_verified=True)
        outsider = User(name="Outsider", email=f"outsider-{sfx}@example.com", hashed_password="x",
                        is_active=True, is_superuser=False, is_verified=True)
        other_org = Organization(name=f"Other {sfx}")
        db.add_all([member, outsider, other_org])
        await db.flush()
        db.add(Membership(user_id=member.id, organization_id=report.organization_id, role="member"))
        db.add(Membership(user_id=outsider.id, organization_id=other_org.id, role="member"))
        await db.commit()
        return {"owner": owner, "member": member, "outsider": outsider, "anonymous": None}


async def _give_viewer_result(report_id: str, query_id: str, user) -> None:
    """A per-viewer run (step_user_results) the member made of one query —
    no API produces one without executing a data source."""
    from app.models.query import Query
    async with async_session_maker() as db:
        query = await db.get(Query, query_id)
        report = await db.get(Report, report_id)
        db.add(StepUserResult(
            step_id=query.default_step_id, user_id=user.id,
            organization_id=report.organization_id, report_id=report_id,
            status="success", data={"rows": [{"k": "MEMBER_OWN_ROW"}], "columns": [{"field": "k"}]},
            last_run_at=datetime.utcnow(), executed_as="viewer",
        ))
        await db.commit()


async def _per_step_view(report_id: str, user, artifact_id=None):
    """What the old two-step flow served: the list, then each query's step."""
    svc = ReportService()
    async with async_session_maker() as db:
        queries = await svc.get_public_queries(db, report_id, artifact_id=artifact_id, user=user)
        out = {}
        for q in queries:
            try:
                step = await svc.get_public_step(db, report_id, q.id, user=user)
                step_json = step.model_dump(mode="json")
            except HTTPException as exc:
                assert exc.status_code == 404
                step_json = None
            out[q.id] = (q.model_dump(mode="json"), step_json)
        return out


async def _batched_view(report_id: str, user, artifact_id=None):
    svc = ReportService()
    async with async_session_maker() as db:
        rows = await svc.get_public_queries_with_steps(db, report_id, artifact_id=artifact_id, user=user)
        out = {}
        for r in rows:
            d = r.model_dump(mode="json")
            step = d.pop("default_step")
            out[d["id"]] = (d, step)
        return out


async def _mixed_public_report():
    """Identity-scoped query (withheld from everyone but the owner) + a plain
    one, on a public dashboard."""
    report_id, artifact_id = await _seed(parameters=IDENTITY_PARAMS, artifact_visibility="public")
    plain_qid = await _add_plain_query(report_id, artifact_id)
    return report_id, artifact_id, plain_qid


@pytest.mark.asyncio
@pytest.mark.parametrize("reader", ["anonymous", "owner", "member", "outsider"])
async def test_batched_steps_equal_the_per_step_view_for_every_reader(reader):
    report_id, artifact_id, _ = await _mixed_public_report()
    user = (await _readers(report_id))[reader]

    for artifact_filter in (None, artifact_id):
        expected = await _per_step_view(report_id, user, artifact_filter)
        got = await _batched_view(report_id, user, artifact_filter)
        assert expected, "precondition: the dashboard has queries"
        assert got == expected


@pytest.mark.asyncio
async def test_batched_view_withholds_identity_rows_from_non_owners_and_keeps_them_for_the_owner():
    report_id, _, plain_qid = await _mixed_public_report()
    readers = await _readers(report_id)

    owner_view = await _batched_view(report_id, readers["owner"])
    for qid, (_, step) in owner_view.items():
        assert step["data"]["rows"], "the owner keeps every snapshot"
        assert step["snapshot_withheld"] is False

    for name in ("anonymous", "member", "outsider"):
        view = await _batched_view(report_id, readers[name])
        for qid, (_, step) in view.items():
            if qid == plain_qid:
                assert step["data"]["rows"] and not step["snapshot_withheld"]
            else:
                assert not step["data"].get("rows") and step["snapshot_withheld"] is True, name
                assert not step["code"], "a withheld reader gets no SQL either"


@pytest.mark.asyncio
async def test_batched_view_serves_a_members_own_run_like_the_per_step_view():
    report_id, _, plain_qid = await _mixed_public_report()
    readers = await _readers(report_id)
    await _give_viewer_result(report_id, plain_qid, readers["member"])

    expected = await _per_step_view(report_id, readers["member"])
    got = await _batched_view(report_id, readers["member"])
    assert got == expected
    assert got[plain_qid][1]["data"]["rows"] == [{"k": "MEMBER_OWN_ROW"}]
    assert got[plain_qid][1]["viewer_result"]["status"] == "success"
    # Nobody else sees the member's run.
    other = await _batched_view(report_id, readers["owner"])
    assert other[plain_qid][1]["data"]["rows"] != [{"k": "MEMBER_OWN_ROW"}]


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility,reader,status", [
    ("none", "anonymous", 401),
    ("none", "outsider", 403),
    ("internal", "anonymous", 401),
    ("internal", "outsider", 403),
])
async def test_batched_view_is_gated_like_the_list_route(visibility, reader, status):
    report_id, _ = await _seed(artifact_visibility=visibility)
    user = (await _readers(report_id))[reader]
    svc = ReportService()
    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as list_exc:
            await svc.get_public_queries(db, report_id, user=user)
    async with async_session_maker() as db:
        with pytest.raises(HTTPException) as batched_exc:
            await svc.get_public_queries_with_steps(db, report_id, user=user)
    assert batched_exc.value.status_code == list_exc.value.status_code
    assert batched_exc.value.status_code in (status, 404)


@pytest.mark.asyncio
async def test_batched_route_is_served_over_http(test_client):
    report_id, artifact_id, plain_qid = await _mixed_public_report()
    resp = test_client.get(f"/api/r/{report_id}/queries_with_steps", params={"artifact_id": artifact_id})
    assert resp.status_code == 200
    body = resp.json()
    by_id = {q["id"]: q for q in body}
    assert plain_qid in by_id and len(body) == 2
    assert by_id[plain_qid]["default_step"]["data"]["rows"] == [{"k": "PLAIN_ROW"}]

    missing = test_client.get(f"/api/r/{uuid.uuid4()}/queries_with_steps")
    assert missing.status_code == 404
