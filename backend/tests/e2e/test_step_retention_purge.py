"""
Reproduction + regression guard: the step-payload retention purge must never
strip data from a report that is shared in ANY mode.

The daily maintenance job (app/services/maintenance_service.py,
purge_step_payloads_for_organization) nulls steps.data/data_model/view after
`step_retention_days`. It excludes shared reports — but only via the legacy
fields (`reports.status = 'published'`, `conversation_share_enabled`), which
are mere side-effects kept in sync by set_visibility(). The actual sharing
source of truth is `artifact_visibility` / `conversation_visibility`
(app/models/report.py). If the legacy sync is missing (historic rows, direct
writes, or when the deprecated fields are eventually dropped), a publicly
shared artifact dashboard silently loses its data after the retention window.

Invariant asserted here: a report with artifact_visibility != 'none' or
conversation_visibility != 'none' keeps its step payloads regardless of the
legacy columns; a fully private draft report is still purged (the by-design
behavior: "restore anytime by rerunning").

Run:
    cd backend
    uv run pytest tests/e2e/test_step_retention_purge.py -v
"""
import asyncio
import uuid
from datetime import datetime, timedelta

import pytest

from app.dependencies import async_session_maker
from app.models.report import Report
from app.models.widget import Widget
from app.models.query import Query
from app.models.step import Step
from app.services.maintenance_service import purge_step_payloads_for_organization

RETENTION_DAYS = 14


def _run(coro):
    return asyncio.run(coro)


async def _seed_report_with_stale_steps(org_id: str, user_id: str, **report_overrides):
    """Create a report whose query has two stale success steps (older than the
    retention cutoff), both still holding payloads.

    Visibility/status combinations that set_visibility() can't produce (e.g.
    artifact_visibility='public' with legacy status left at 'draft') are the
    exact drift this test guards against, so the rows are written directly.
    """
    suffix = uuid.uuid4().hex[:8]
    stale = datetime.utcnow() - timedelta(days=RETENTION_DAYS + 10)

    async with async_session_maker() as db:
        report = Report(
            title=f"Retention {suffix}",
            slug=f"retention-{suffix}",
            user_id=user_id,
            organization_id=org_id,
            status="draft",
            **report_overrides,
        )
        db.add(report)
        await db.flush()

        widget = Widget(title=f"W {suffix}", slug=f"w-{suffix}", report_id=report.id)
        db.add(widget)
        await db.flush()

        query = Query(
            title="Q",
            report_id=report.id,
            widget_id=widget.id,
            organization_id=org_id,
            user_id=user_id,
        )
        db.add(query)
        await db.flush()

        step_ids = []
        for si in range(2):
            step = Step(
                title=f"S{si}",
                slug=f"s{si}-{suffix}",
                status="success",
                widget_id=widget.id,
                query_id=query.id,
                code="def generate_df(ds_clients, excel_files): ...",
                data={"rows": [{"v": si}], "columns": [{"field": "v"}]},
                data_model={"type": "table"},
                view={"type": "table"},
                created_at=stale + timedelta(hours=si),
                updated_at=stale + timedelta(hours=si),
            )
            db.add(step)
            await db.flush()
            step_ids.append(str(step.id))
        query.default_step_id = step_ids[-1]
        await db.commit()

        return {"report_id": str(report.id), "step_ids": step_ids}


async def _step_payloads(step_ids):
    async with async_session_maker() as db:
        out = {}
        for sid in step_ids:
            step = await db.get(Step, sid)
            out[sid] = step.data
        return out


def _org_and_user_via_api(create_user, login_user, whoami):
    """Real org + user through the registration/onboarding endpoints, so the
    purge runs against production-shaped rows (settings, memberships, roles)."""
    user = create_user()
    token = login_user(user["email"], user["password"])
    me = whoami(token)
    return me["organizations"][0]["id"], me["id"]


@pytest.mark.e2e
def test_purge_never_touches_shared_reports_even_without_legacy_field_sync(
    create_user, login_user, whoami
):
    org_id, user_id = _org_and_user_via_api(create_user, login_user, whoami)

    async def scenario():

        artifact_shared = await _seed_report_with_stale_steps(
            org_id, user_id, artifact_visibility="public")
        conversation_shared = await _seed_report_with_stale_steps(
            org_id, user_id, conversation_visibility="shared")
        private = await _seed_report_with_stale_steps(org_id, user_id)

        await purge_step_payloads_for_organization(org_id, retention_days=RETENTION_DAYS)

        return {
            "artifact_shared": await _step_payloads(artifact_shared["step_ids"]),
            "conversation_shared": await _step_payloads(conversation_shared["step_ids"]),
            "private": await _step_payloads(private["step_ids"]),
        }

    payloads = _run(scenario())

    # Shared reports (either mode) keep every step payload — sharing is
    # determined by the visibility columns, not the legacy status sync.
    for kind in ("artifact_shared", "conversation_shared"):
        for sid, data in payloads[kind].items():
            assert data is not None and data.get("rows"), (
                f"{kind}: step {sid} payload was purged from a shared report")

    # The fully private stale draft is still purged — both the older version
    # and the stale latest one (restorable by rerunning).
    assert all(data is None for data in payloads["private"].values()), (
        "private stale draft report should still be purged")


@pytest.mark.e2e
def test_purge_still_respects_legacy_published_and_share_flags(
    create_user, login_user, whoami
):
    """The legacy exclusions must keep working while the old fields exist."""
    org_id, user_id = _org_and_user_via_api(create_user, login_user, whoami)

    async def scenario():
        published = await _seed_report_with_stale_steps(org_id, user_id)
        convo_flagged = await _seed_report_with_stale_steps(org_id, user_id)

        async with async_session_maker() as db:
            r1 = await db.get(Report, published["report_id"])
            r1.status = "published"
            r2 = await db.get(Report, convo_flagged["report_id"])
            r2.conversation_share_enabled = True
            await db.commit()

        await purge_step_payloads_for_organization(org_id, retention_days=RETENTION_DAYS)

        return {
            "published": await _step_payloads(published["step_ids"]),
            "convo_flagged": await _step_payloads(convo_flagged["step_ids"]),
        }

    payloads = _run(scenario())
    for kind, steps in payloads.items():
        for sid, data in steps.items():
            assert data is not None and data.get("rows"), (
                f"{kind}: step {sid} payload was purged despite legacy share flag")
