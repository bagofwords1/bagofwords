"""find_viewer_result: the cache lookup every viewer run does before deciding to run.

Contract asserted here:
  1. it returns the viewer's stored row for exactly this step + parameter set,
     and None when there is none;
  2. it does not load the related Step or User. Both are lazy="selectin" and
     fan out through Step.query to the report graph; a plain select sent ~44
     statements (~50 ms) on every run that finds a stored row, for relations
     the callers never read (status, last_run_at and data are columns).

Steps have no public CRUD API (the AI flow creates them), so the step is
seeded directly, as in test_report_rerun_params.py.

Run:
    cd backend
    uv run pytest tests/unit/test_viewer_result_lookup.py -v
"""
import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import event, inspect

from app.dependencies import async_session_maker
from app.models.report import Report
from app.models.step import Step
from app.services.viewer_result_store import find_viewer_result, store_viewer_result

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py


def _run(coro):
    return asyncio.run(coro)


def _frame(n):
    return {"rows": [{"v": i} for i in range(n)], "columns": [{"field": "v"}]}


async def _seed(report_id):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        step = Step(title="S", slug=f"s-{uuid.uuid4().hex[:8]}", status="success",
                    code="", data=_frame(50))
        db.add(step)
        await db.commit()
        world = {"step_id": str(step.id), "user_id": str(report.user_id),
                 "org_id": str(report.organization_id)}
        for fp, n in (("fp-a", 3), ("fp-b", 1)):
            await store_viewer_result(
                db, step_id=world["step_id"], user_id=world["user_id"], params_fingerprint=fp,
                organization_id=world["org_id"], report_id=None, status="success",
                status_reason=None, executed_as="viewer", applied_params={"fp": fp},
                data=_frame(n), last_run_at=datetime.utcnow())
        return world


async def _find_and_watch(world, fingerprint):
    """Call find_viewer_result in a fresh session, recording every statement it sends."""
    statements = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    async with async_session_maker() as db:
        engine = db.get_bind()
        event.listen(engine, "before_cursor_execute", _record)
        try:
            row = await find_viewer_result(db, step_id=world["step_id"], user_id=world["user_id"],
                                           params_fingerprint=fingerprint)
        finally:
            event.remove(engine, "before_cursor_execute", _record)
        found = None if row is None else {
            "fingerprint": row.params_fingerprint, "status": row.status, "data": row.data,
            "unloaded": inspect(row).unloaded,
        }
    return found, statements


@pytest.fixture
def world(create_report, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Viewer lookup", user_token=token, org_id=org_id,
                           data_sources=[])
    return _run(_seed(report["id"]))


def test_finds_the_row_for_this_parameter_set_without_loading_step_or_user(world):
    found, statements = _run(_find_and_watch(world, "fp-a"))

    assert found["fingerprint"] == "fp-a"
    assert found["status"] == "success"
    assert found["data"] == _frame(3)
    assert {"step", "user"} <= found["unloaded"]
    # The lookup itself, nothing else: no steps / users / queries / reports fan-out.
    assert len(statements) <= 1, statements
    assert all("step_user_results" in s.lower() for s in statements), statements


def test_other_parameter_sets_and_missing_rows(world):
    other, _ = _run(_find_and_watch(world, "fp-b"))
    missing, statements = _run(_find_and_watch(world, "fp-none"))

    assert other["fingerprint"] == "fp-b" and other["data"] == _frame(1)
    assert missing is None
    assert all("step_user_results" in s.lower() for s in statements), statements
