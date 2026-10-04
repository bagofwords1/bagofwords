"""Forking follows per-dashboard sharing.

A viewer of one dashboard of a report could fork the whole report and so get
every other, private dashboard with its queries and rows in a report of their
own. The fork now copies what the forker may open:

- a viewer of dashboard A gets A, A's queries (and the query its filter
  draws options from) and nothing of B: no artifact, query, widget, title or
  row of B, in the copy or in the fork's summary message;
- the owner gets every live dashboard, not only the newest one;
- an org member none of it is shared with cannot fork.

Run:
    cd backend
    TESTING=true uv run pytest tests/e2e/rbac/test_per_dashboard_fork.py --db=sqlite
"""
import asyncio
import json

import pytest
from sqlalchemy import select

from app.dependencies import async_session_maker
from app.models.artifact import Artifact
from app.models.completion import Completion
from app.models.query import Query
from app.models.step import Step
from app.models.widget import Widget
from tests.e2e.rbac.test_per_dashboard_data_scope import _headers, scoped  # noqa: F401  (fixture)


async def _fork_contents(fork_id):
    async with async_session_maker() as db:
        artifacts = (await db.execute(
            select(Artifact.title).where(Artifact.report_id == fork_id, Artifact.deleted_at.is_(None))
        )).scalars().all()
        queries = (await db.execute(select(Query.title).where(Query.report_id == fork_id))).scalars().all()
        widgets = (await db.execute(select(Widget.title).where(Widget.report_id == fork_id))).scalars().all()
        rows = (await db.execute(
            select(Step.data).join(Query, Query.id == Step.query_id).where(Query.report_id == fork_id)
        )).scalars().all()
        summary = (await db.execute(
            select(Completion.completion, Completion.fork_asset_refs).where(Completion.report_id == fork_id)
        )).all()
        return set(artifacts), set(queries), set(widgets), json.dumps(rows), json.dumps(
            [list(r) for r in summary], default=str)


def _fork(test_client, rid, token, org_id):
    return test_client.post(f"/api/reports/{rid}/fork", json={}, headers=_headers(token, org_id))


@pytest.mark.e2e
def test_viewer_of_one_dashboard_forks_only_that_dashboard(test_client, scoped):
    c = scoped
    resp = _fork(test_client, c["rid"], c["viewer"]["token"], c["org_id"])
    assert resp.status_code == 200, resp.json()

    artifacts, queries, widgets, rows, summary = asyncio.run(_fork_contents(resp.json()["id"]))
    assert artifacts == {"Sales"}
    assert queries == {"Sales", "Regions"}
    assert widgets == {"Sales", "Regions"}
    assert "Payroll" not in rows
    assert "Payroll" not in summary


@pytest.mark.e2e
def test_owner_forks_every_dashboard(test_client, scoped):
    c = scoped
    resp = _fork(test_client, c["rid"], c["owner_token"], c["org_id"])
    assert resp.status_code == 200, resp.json()

    artifacts, queries, _, _, _ = asyncio.run(_fork_contents(resp.json()["id"]))
    assert artifacts == {"Sales", "Payroll"}
    assert queries == {"Sales", "Regions", "Payroll"}


@pytest.mark.e2e
def test_member_with_nothing_shared_cannot_fork(test_client, scoped, invite_user_to_org):
    c = scoped
    outsider = invite_user_to_org(org_id=c["org_id"], admin_token=c["owner_token"])
    assert _fork(test_client, c["rid"], outsider["token"], c["org_id"]).status_code == 403
