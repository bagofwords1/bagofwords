"""store_viewer_result: the per-viewer upsert and the row it hands back.

Contract asserted here:
  1. the returned row carries the values just written, also when the session
     already held that row with older values (the upsert bypasses the ORM);
  2. the same (step, user, fingerprint) slot is updated in place, and another
     fingerprint gets its own row;
  3. handing the row back does not load the related Step or User. Both are
     lazy="selectin", and Step carries the stored result data, so loading
     them cost ~75 ms per viewer run on a large step for relations no caller
     reads.

Steps have no public CRUD API (the AI flow creates them), so the step is
seeded directly, as in test_report_rerun_params.py.

Run:
    cd backend
    uv run pytest tests/unit/test_viewer_result_store.py -v
"""
import asyncio
import uuid
from datetime import datetime

import pytest
from sqlalchemy import event, func, inspect, select

from app.dependencies import async_session_maker
from app.models.report import Report
from app.models.step import Step
from app.models.step_user_result import StepUserResult
from app.services.viewer_result_store import store_viewer_result

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py

# Every statement storing a result may touch only step_user_results: the
# upsert and the re-select. Anything else (steps, users, queries, reports...)
# is the relationship fan-out this guards against.
def _other_tables(statements):
    return [s for s in statements if "step_user_results" not in s.lower()]


def _run(coro):
    return asyncio.run(coro)


def _frame(n):
    return {"rows": [{"v": i} for i in range(n)], "columns": [{"field": "v"}]}


async def _seed_step(report_id):
    async with async_session_maker() as db:
        report = await db.get(Report, report_id)
        step = Step(title="S", slug=f"s-{uuid.uuid4().hex[:8]}", status="success",
                    code="", data=_frame(50))
        db.add(step)
        await db.commit()
        return {"step_id": str(step.id), "user_id": str(report.user_id),
                "org_id": str(report.organization_id)}


def _values(world, **over):
    return {"organization_id": world["org_id"], "report_id": None,
            "status": "success", "status_reason": None, "executed_as": "viewer",
            "applied_params": None, "last_run_at": datetime.utcnow(), **over}


async def _store_and_watch(db, world, fingerprint, **values):
    """Call store_viewer_result, recording every statement it sends."""
    statements = []

    def _record(conn, cursor, statement, *args):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", _record)
    try:
        row = await store_viewer_result(
            db, step_id=world["step_id"], user_id=world["user_id"],
            params_fingerprint=fingerprint, **_values(world, **values))
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    return row, statements


@pytest.fixture
def world(create_report, create_user, login_user, whoami):
    user = create_user()
    token = login_user(user["email"], user["password"])
    org_id = whoami(token)["organizations"][0]["id"]
    report = create_report(title="Viewer store", user_token=token, org_id=org_id,
                           data_sources=[])
    return _run(_seed_step(report["id"]))


def test_returned_row_has_new_values_without_loading_step_or_user(world):
    async def go():
        async with async_session_maker() as db:
            row, statements = await _store_and_watch(
                db, world, "fp-a", data=_frame(3), applied_params={"Year": "2023"})
            state = inspect(row)
            return ({"status": row.status, "data": row.data,
                     "applied_params": row.applied_params,
                     "fingerprint": row.params_fingerprint},
                    state.unloaded, statements)

    values, unloaded, statements = _run(go())

    assert values == {"status": "success", "data": _frame(3),
                      "applied_params": {"Year": "2023"}, "fingerprint": "fp-a"}
    assert {"step", "user"} <= unloaded
    assert _other_tables(statements) == [], statements
    assert 1 <= len(statements) <= 2, statements  # at most the upsert and one read back


def test_upsert_updates_the_slot_and_refreshes_a_held_instance(world):
    async def go():
        async with async_session_maker() as db:
            first, _ = await _store_and_watch(db, world, "fp-a", data=_frame(2),
                                              applied_params={"Year": "2023"})
            first_id = first.id
            # Same slot, new values, same session: the instance the session
            # already holds must come back with the new values.
            second, statements = await _store_and_watch(
                db, world, "fp-a", status="error", status_reason="boom",
                data=None, applied_params={"Year": "2024"})
            same_slot = {"same_object": second is first, "same_id": second.id == first_id,
                         "status": second.status, "reason": second.status_reason,
                         "data": second.data, "applied_params": second.applied_params}
            other, _ = await _store_and_watch(db, world, "fp-b", data=_frame(1))
            other_slot = {"distinct_id": other.id != first_id, "data": other.data}
            count = (await db.execute(
                select(func.count()).select_from(StepUserResult)
                .where(StepUserResult.step_id == world["step_id"])
            )).scalar_one()
            return same_slot, other_slot, count, statements

    same_slot, other_slot, count, statements = _run(go())

    assert same_slot == {"same_object": True, "same_id": True, "status": "error",
                         "reason": "boom", "data": None,
                         "applied_params": {"Year": "2024"}}
    assert other_slot == {"distinct_id": True, "data": _frame(1)}
    assert count == 2
    assert _other_tables(statements) == [], statements
