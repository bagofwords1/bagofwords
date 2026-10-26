"""Chat on a shared dashboard answers from the VIEWER's rows, never the creator's.

`/r/{id}` chat injects the dashboard's own data into the planner prompt
(data-only scope) and lets the agent read saved queries by id (agents scope).
Both read saved steps, whose `Step.data` snapshot was materialized under the
report owner's identity and credentials — on an identity-scoped dashboard that
snapshot is ONE tenant's row slice.

Contract pinned here: every row that reaches the chat prompt or the read_query
tool is resolved through the same `viewer_data_policy.resolve_step_data` the
dashboard itself goes through, so chat can never answer customer A out of
customer B's rows — the surface that shows the data and the surface that talks
about it agree, reader by reader.
"""
import asyncio
import uuid

import pytest
from sqlalchemy import select

from app.ai.agent_v2 import AgentV2
from app.dependencies import async_session_maker
from app.models.membership import Membership
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.user import User
from app.services.artifact_chat_service import artifact_chat_service
from app.services.report_service import ReportService
from tests.e2e.test_html_export_offline import _seed

pytestmark = pytest.mark.e2e

IDENTITY_PARAMS = [{"name": "email", "type": "string", "source": "identity"}]
INPUT_DEFAULT_PARAMS = [{"name": "owner", "type": "string", "source": "input_identity_default"}]
PLAIN_PARAMS = []


def _run(coro):
    return asyncio.run(coro)


async def _owner_of(report_id: str) -> User:
    """The report's owner, as a member of its org (the seed helper makes the
    user and the org separately; 'internal' sharing reads memberships)."""
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        owner = await db.get(User, report.user_id)
        existing = (await db.execute(
            select(Membership.id).where(
                Membership.user_id == owner.id,
                Membership.organization_id == report.organization_id,
            )
        )).scalars().first()
        if existing is None:
            db.add(Membership(user_id=owner.id, organization_id=report.organization_id,
                              role="admin"))
            await db.commit()
        return owner


async def _a_member_of(report_id: str) -> User:
    """A second org member — the dashboard's viewer.

    Seeded directly: the second user in this scenario needs no API surface of
    their own, and the report fixture this test builds on is itself DB-seeded
    (there is no endpoint that creates a saved query + artifact without an
    LLM run).
    """
    sfx = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        user = User(name="Viewer", email=f"chat-viewer-{sfx}@example.com", hashed_password="x",
                    is_active=True, is_superuser=False, is_verified=True)
        db.add(user)
        await db.flush()
        db.add(Membership(user_id=user.id, organization_id=report.organization_id, role="member"))
        await db.commit()
        return user


async def _chat_rows(report_id: str, viewer) -> list[dict]:
    """The visualizations the data-only chat prompt would carry for `viewer`."""
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        ctx = await artifact_chat_service.build_platform_context(db, report, [], viewer=viewer)
        return ctx.get("visualizations") or []


async def _dashboard_rows(report_id: str, viewer) -> list[list[dict]]:
    """What the dashboard's own read path serves `viewer`, query by query."""
    svc = ReportService()
    async with async_session_maker() as db:
        qids = [r[0] for r in (await db.execute(
            select(Query.id).where(Query.report_id == report_id)
        )).all()]
        out = []
        for qid in qids:
            step = await svc.get_public_step(db, report_id, str(qid), user=viewer)
            out.append((step.data or {}).get("rows", []))
        return out


async def _record_viewer_run(report_id: str, viewer: User, rows: list[dict]) -> None:
    """Give `viewer` a result of their own for the dashboard's step.

    Direct write: producing one through QueryService would need a live data
    source, and what is under test is which rows a reader is served, not how
    their run was produced.
    """
    from datetime import UTC, datetime

    from app.models.step_user_result import StepUserResult

    async with async_session_maker() as db:
        step_id = (await db.execute(
            select(Step.id).join(Query, Step.query_id == Query.id)
            .where(Query.report_id == report_id)
        )).scalars().first()
        org_id = (await db.get(Report, report_id)).organization_id
        db.add(StepUserResult(
            step_id=str(step_id), user_id=str(viewer.id), status="success",
            organization_id=str(org_id), report_id=str(report_id),
            data={"rows": rows, "columns": [{"field": "region"}, {"field": "amount"}]},
            params_fingerprint="", executed_as="viewer",
            last_run_at=datetime.now(UTC),
        ))
        await db.commit()


@pytest.mark.parametrize("params", [IDENTITY_PARAMS, INPUT_DEFAULT_PARAMS])
def test_chat_withholds_the_creator_snapshot_from_another_viewer(params):
    """An identity-scoped dashboard: the viewer's chat gets no rows at all."""
    report_id, _ = _run(_seed(parameters=params, artifact_visibility="internal"))
    viewer = _run(_a_member_of(report_id))

    viz = _run(_chat_rows(report_id, viewer))
    assert viz, "the dashboard's visualization should still be described"
    assert all(v["rows"] == [] for v in viz)
    assert all(v["columns"] == [] for v in viz)
    assert all(v["withheld"] is True for v in viz)
    assert all(v["total_rows"] == 0 for v in viz)


@pytest.mark.parametrize("params", [IDENTITY_PARAMS, INPUT_DEFAULT_PARAMS, PLAIN_PARAMS])
def test_chat_rows_are_exactly_what_the_dashboard_serves_the_same_reader(params):
    """The invariant, for every reader and every dashboard shape."""
    report_id, _ = _run(_seed(parameters=params, artifact_visibility="internal"))
    owner = _run(_owner_of(report_id))
    viewer = _run(_a_member_of(report_id))

    for reader in (owner, viewer):
        chat = [v["rows"] for v in _run(_chat_rows(report_id, reader))]
        assert chat == _run(_dashboard_rows(report_id, reader)), f"drift for {reader.name}"


def test_chat_serves_a_viewer_their_own_run_not_the_creators():
    """Once the viewer has run it themselves, chat answers from THEIR slice."""
    report_id, _ = _run(_seed(parameters=IDENTITY_PARAMS, artifact_visibility="internal"))
    viewer = _run(_a_member_of(report_id))
    mine = [{"region": "NL", "amount": 7}]
    _run(_record_viewer_run(report_id, viewer, mine))

    viz = _run(_chat_rows(report_id, viewer))
    assert [v["rows"] for v in viz] == [mine]
    assert all(v["withheld"] is False for v in viz)


def test_chat_keeps_serving_a_plain_dashboard_to_its_viewers():
    """No identity in play: sharing a dashboard still shares its numbers."""
    report_id, _ = _run(_seed(parameters=PLAIN_PARAMS, artifact_visibility="internal"))
    owner = _run(_owner_of(report_id))
    viewer = _run(_a_member_of(report_id))

    owner_rows = [v["rows"] for v in _run(_chat_rows(report_id, owner))]
    assert owner_rows and owner_rows[0]
    assert [v["rows"] for v in _run(_chat_rows(report_id, viewer))] == owner_rows


# --- the wiring: the route must hand the viewer to the context builder -------

def _headers(token, org_id):
    return {"Authorization": f"Bearer {token}", "X-Organization-Id": str(org_id)}


def _capture_platform_context(monkeypatch) -> dict:
    """Stub the agent loop and keep the platform_context it was constructed with."""
    seen: dict = {}

    async def fake_main_execution(self):
        seen["platform_context"] = self.platform_context
        self.system_completion.status = "success"
        self.db.add(self.system_completion)
        await self.db.commit()

    monkeypatch.setattr(AgentV2, "main_execution", fake_main_execution)
    return seen


def test_chat_route_resolves_the_dashboard_data_for_the_sender(
    monkeypatch, test_client, create_user, login_user, whoami,
    create_llm_provider_and_models, create_report,
):
    monkeypatch.setenv("OPENAI_API_KEY_TEST", "sk-test-dummy")
    owner = create_user(email=f"chat-owner-{uuid.uuid4().hex[:10]}@example.com")
    owner_token = login_user(owner["email"], owner["password"])
    org_id = whoami(owner_token)["organizations"][0]["id"]
    create_llm_provider_and_models(owner_token, org_id)
    report = create_report(title=f"chat-data-{uuid.uuid4().hex[:6]}",
                           user_token=owner_token, org_id=org_id, data_sources=[])
    rid = report["id"]
    _run(_seed_identity_dashboard_into(rid))

    assert test_client.put(
        f"/api/reports/{rid}/visibility/artifact",
        json={"visibility": "internal", "artifact_chat_enabled": True,
              "artifact_chat_data_source_ids": []},
        headers=_headers(owner_token, org_id),
    ).status_code == 200

    member_email = f"chat-member-{uuid.uuid4().hex[:10]}@example.com"
    assert test_client.post(
        f"/api/organizations/{org_id}/members",
        json={"organization_id": org_id, "email": member_email, "role": "member"},
        headers=_headers(owner_token, org_id),
    ).status_code == 200
    member = create_user(email=member_email, password="test123")
    member_token = login_user(member["email"], member["password"])

    seen = _capture_platform_context(monkeypatch)
    assert test_client.post(
        f"/api/r/{rid}/chat/completions",
        json={"prompt": {"content": "what are the numbers?", "mentions": [{}]}},
        headers=_headers(member_token, org_id),
    ).status_code == 200
    viz = (seen["platform_context"] or {}).get("visualizations") or []
    assert viz, "data-only chat should still describe the dashboard"
    assert all(v["rows"] == [] and v["withheld"] is True for v in viz)

    seen.clear()
    assert test_client.post(
        f"/api/r/{rid}/chat/completions",
        json={"prompt": {"content": "what are the numbers?", "mentions": [{}]}},
        headers=_headers(owner_token, org_id),
    ).status_code == 200
    owner_viz = (seen["platform_context"] or {}).get("visualizations") or []
    assert owner_viz and owner_viz[0]["rows"], "the owner still sees their own dashboard"


async def _seed_identity_dashboard_into(report_id: str) -> None:
    """Add one identity-scoped visualization + artifact to an API-made report."""
    from app.models.visualization import Visualization
    from app.models.widget import Widget
    from tests.fixtures.artifact import seed_artifact

    sfx = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        widget = Widget(title="Sales", slug=f"w-{sfx}", report_id=report.id)
        db.add(widget)
        await db.flush()
        step = Step(
            title="Sales step", slug=f"s-{sfx}", status="success", widget_id=widget.id,
            data={"rows": [{"region": "US", "amount": 10}],
                  "columns": [{"field": "region"}, {"field": "amount"}]},
            data_model={"type": "table"},
        )
        db.add(step)
        await db.flush()
        query = Query(title="Sales", report_id=report.id, widget_id=widget.id,
                      default_step_id=step.id, parameters=IDENTITY_PARAMS,
                      organization_id=report.organization_id)
        db.add(query)
        await db.flush()
        step.query_id = query.id
        await db.flush()
        viz = Visualization(title="Sales", status="success", report_id=report.id,
                            query_id=query.id, view={"type": "bar_chart"})
        db.add(viz)
        await db.flush()
        await seed_artifact(
            db, report_id=report.id, user_id=report.user_id,
            organization_id=report.organization_id, mode="page", title="Dashboard",
            content={"code": "export default function D(){return null}",
                     "visualization_ids": [str(viz.id)]},
        )
        await db.commit()


# --- the other row surface chat has: read_query on a saved step -------------

async def _stamp_query_org(report_id: str) -> None:
    """read_query scopes its lookup to the caller's org; the shared seed
    helper leaves Query.organization_id unset, which would make every lookup
    here fail for the wrong reason."""
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        for q in (await db.execute(
            select(Query).where(Query.report_id == report_id)
        )).scalars().all():
            q.organization_id = report.organization_id
        await db.commit()


async def _read_query_as(report_id: str, user) -> dict:
    """Run the read_query tool over the dashboard's saved query, as `user`.

    Mirrors how artifact chat reaches it: by query id, from a conversation
    that is not the dashboard's own report.
    """
    from app.ai.tools.implementations.read_query import ReadQueryTool
    from app.models.organization import Organization

    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        organization = await db.get(Organization, report.organization_id)
        qid = (await db.execute(
            select(Query.id).where(Query.report_id == report_id)
        )).scalars().first()
        ctx = {"db": db, "organization": organization, "report": None, "user": user}
        events = [e async for e in ReadQueryTool().run_stream({"query_ids": [str(qid)]}, ctx)]
        return events[-1].payload["output"]


def test_read_query_withholds_a_saved_step_from_another_viewer():
    report_id, _ = _run(_seed(parameters=IDENTITY_PARAMS, artifact_visibility="internal"))
    _run(_stamp_query_org(report_id))
    viewer = _run(_a_member_of(report_id))

    out = _run(_read_query_as(report_id, viewer))
    assert out["success"] is False
    result = out["results"][0]
    assert not result["data"]
    assert not result["data_preview"]
    assert not result["code"]
    assert result["error"]


def test_read_query_still_serves_the_owner_and_plain_dashboards():
    report_id, _ = _run(_seed(parameters=IDENTITY_PARAMS, artifact_visibility="internal"))
    _run(_stamp_query_org(report_id))
    owner = _run(_owner_of(report_id))
    owner_out = _run(_read_query_as(report_id, owner))
    assert owner_out["success"] is True, owner_out["results"]
    assert owner_out["results"][0]["data"]["rows"]

    plain_id, _ = _run(_seed(parameters=PLAIN_PARAMS, artifact_visibility="internal"))
    _run(_stamp_query_org(plain_id))
    member = _run(_a_member_of(plain_id))
    plain_out = _run(_read_query_as(plain_id, member))
    assert plain_out["success"] is True
    assert plain_out["results"][0]["data"]["rows"]
