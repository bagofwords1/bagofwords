"""Unit tests for server-side eval run finalization.

These cover ``TestRunService._maybe_finalize_run`` — the session-safe
aggregate that closes out a ``TestRun`` once every one of its results is
terminal. It's the core of making eval runs self-finalizing on the server
(so a run no longer sits in ``in_progress`` until a client opens the run
page) and it fixes the stale-session bug where a run persisted
``status=success`` while one of its results was ``fail``.

No LLM is required: the tests drive ``_maybe_finalize_run`` directly against
handcrafted ``TestRun`` / ``TestResult`` rows.

Run:
    BOW_DATABASE_URL="sqlite:///db/app.db" \
      python -m pytest tests/e2e/test_eval_run_finalize.py -v
"""
import uuid
from datetime import datetime

import pytest

from app.dependencies import async_session_maker
from app.models.eval import TestRun, TestResult
from app.services.test_run_service import TestRunService


def _mk_result(run_id: str, status: str) -> TestResult:
    return TestResult(
        run_id=run_id,
        case_id=str(uuid.uuid4()),
        head_completion_id=str(uuid.uuid4()),
        status=status,
        report_id=str(uuid.uuid4()),
        result_json=None,
    )


async def _make_run(statuses, *, run_status: str = "in_progress") -> str:
    async with async_session_maker() as session:
        run = TestRun(
            suite_ids="suite-" + uuid.uuid4().hex[:8],
            title="finalize-test",
            status=run_status,
            started_at=datetime.utcnow(),
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        for s in statuses:
            session.add(_mk_result(str(run.id), s))
        await session.commit()
        return str(run.id)


async def _run_status(run_id: str):
    async with async_session_maker() as session:
        run = await session.get(TestRun, run_id)
        return run.status, run.finished_at, (run.summary_json or {})


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_finalize_noop_while_a_result_is_in_progress():
    svc = TestRunService()
    run_id = await _make_run(["pass", "in_progress"])
    await svc._maybe_finalize_run(async_session_maker, run_id)
    status, finished_at, _ = await _run_status(run_id)
    assert status == "in_progress"
    assert finished_at is None


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_finalize_all_pass_is_success():
    svc = TestRunService()
    run_id = await _make_run(["pass", "pass"])
    await svc._maybe_finalize_run(async_session_maker, run_id)
    status, finished_at, summary = await _run_status(run_id)
    assert status == "success"
    assert finished_at is not None
    assert summary == {"total": 2, "passed": 2, "failed": 0}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_finalize_with_a_fail_is_error_not_success():
    """The stale-session bug: a run with a failing result must NOT persist
    ``success``. ``_maybe_finalize_run`` reads freshly-committed rows, so it
    aggregates the fail correctly."""
    svc = TestRunService()
    run_id = await _make_run(["pass", "fail"])
    await svc._maybe_finalize_run(async_session_maker, run_id)
    status, _, summary = await _run_status(run_id)
    assert status == "error"
    assert summary == {"total": 2, "passed": 1, "failed": 1}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_finalize_with_error_result_is_error():
    svc = TestRunService()
    run_id = await _make_run(["pass", "error"])
    await svc._maybe_finalize_run(async_session_maker, run_id)
    status, _, summary = await _run_status(run_id)
    assert status == "error"
    assert summary == {"total": 2, "passed": 1, "failed": 1}


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_finalize_preserves_stopped():
    svc = TestRunService()
    run_id = await _make_run(["stopped", "pass"])
    await svc._maybe_finalize_run(async_session_maker, run_id)
    status, _, _ = await _run_status(run_id)
    assert status == "stopped"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_finalize_is_idempotent_and_does_not_reopen_terminal_run():
    """A run already marked terminal is left untouched (idempotent), even if
    called again — no accidental status flip."""
    svc = TestRunService()
    run_id = await _make_run(["pass", "pass"], run_status="success")
    await svc._maybe_finalize_run(async_session_maker, run_id)
    await svc._maybe_finalize_run(async_session_maker, run_id)
    status, _, _ = await _run_status(run_id)
    assert status == "success"


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_finalize_no_results_is_noop():
    svc = TestRunService()
    run_id = await _make_run([])
    await svc._maybe_finalize_run(async_session_maker, run_id)
    status, finished_at, _ = await _run_status(run_id)
    assert status == "in_progress"
    assert finished_at is None
