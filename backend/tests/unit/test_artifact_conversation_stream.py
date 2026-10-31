"""The NDJSON stream of an artifact conversation message, and its result lookup.

``artifact_conversations.ndjson`` turns the completion's SSE into the SDK's
events. These tests feed it a fake SSE iterator that yields hand-built frames
(the agent's event boundary) and seed the thread's tool rows with the ORM:
the agent loop is what writes them, and no API can (rule 5).

The contract under test:

* a ``create_data`` result is emitted once, before ``completed``; ``completed``
  waits for a result still being persisted, but only for
  ``RESULT_DEFER_SECONDS`` (patched here: the clock boundary);
* nothing follows ``completed`` or ``error``; a ``completion.error`` after the
  answer finished is ignored; a failing SSE read still ends the stream with
  its terminal line;
* ``results_for`` never runs without a message or tool-execution filter, and
  returns only the thread's own successful ``create_data`` steps;
* rows committed after the SSE closed are still looked for until the deadline;
* when results could not all be shown (a failed lookup, a result still pending
  at the deadline, one lost to the message budget, one that cannot be shaped,
  or every result withheld because the PII settings could not be read)
  ``completed`` and the history message say ``results_incomplete: true``;
  the text answer still flows.

Database faults are injected at the session (``DbFault``: a real session whose
matching statements raise a driver error), PII load failures at the loader's
boundaries (the licence gate and the ruleset build).

No sleep synchronizes anything: the fake SSE waits on events the test sets,
and ``asyncio.timeout(10)`` is only a failure bound.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text as sql_text
from sqlalchemy.exc import OperationalError

import app.ai.llm.pii.loader as pii_loader
import app.dependencies
from app.ai.llm.pii.redactor import build_redactor
from app.dependencies import async_session_maker
from app.models.agent_execution import AgentExecution
from app.models.completion import Completion
from app.models.completion_block import CompletionBlock
from app.models.organization import Organization
from app.models.organization_settings import OrganizationSettings
from app.models.query import Query
from app.models.report import Report
from app.models.step import Step
from app.models.tool_execution import ToolExecution
from app.models.user import User
from app.models.visualization import Visualization
from app.services import artifact_conversations
from app.services.artifact_conversation_results import results_for

pytestmark = pytest.mark.db  # opens real sessions — see tests/unit/conftest.py

LOGGER = "app.services.artifact_conversations"
BOUND = 10
ROWS = [{"genre": g, "sales": s} for g, s in [("Rock", 80.19), ("Jazz", 31.5), ("Blues", 12)]]
GRID = {"rows": ROWS, "columns": [{"headerName": "genre", "field": "genre"}, {"headerName": "sales", "field": "sales"}],
        "info": {"total_rows": 3, "total_columns": 2,
                 "column_info": {"genre": {"dtype": "object"}, "sales": {"dtype": "float64"}}}}
BAR = {"version": "v2", "view": {"type": "bar_chart", "x": "genre", "y": "sales"}}


# ── seeding (the agent's rows) ─────────────────────────────────────────

async def _thread(*, status="in_progress"):
    async with async_session_maker() as db:
        user = User(name="Viewer", email=f"viewer-{uuid.uuid4().hex}@example.test", hashed_password="not-used")
        org = Organization(name=f"Stream org {uuid.uuid4().hex}")
        db.add_all([user, org])
        await db.flush()
        thread = Report(title="Chat", slug=f"artifact-chat-{uuid.uuid4().hex[:12]}", report_type="artifact_chat",
                        user_id=str(user.id), organization_id=str(org.id))
        other = Report(title="Elsewhere", slug=f"elsewhere-{uuid.uuid4().hex[:12]}",
                       user_id=str(user.id), organization_id=str(org.id))
        db.add_all([thread, other])
        await db.flush()
        messages = [Completion(report_id=str(thread.id), user_id=str(user.id), role="system", status=status,
                               prompt={}, completion={}) for _ in range(2)]
        db.add_all(messages)
        await db.commit()
        return SimpleNamespace(org=str(org.id), user=str(user.id), thread=str(thread.id), other=str(other.id),
                               message=str(messages[0].id), second_message=str(messages[1].id))


async def _seed(t, *, te_id=None, message=None, tool_name="create_data", te_status="success", step_status="success",
                query_report=None, execution_report=None, grid=GRID, view=BAR, block_index=1, title="Sales by genre"):
    """What a finished tool leaves behind: the step and its visualization, the
    tool execution, and its block on the message."""
    te_id = te_id or str(uuid.uuid4())
    async with async_session_maker() as db:
        execution = AgentExecution(completion_id=message or t.message, organization_id=t.org, user_id=t.user,
                                   report_id=execution_report or t.thread, status="success")
        query = Query(title=title, report_id=query_report or t.thread, organization_id=t.org, user_id=t.user)
        db.add_all([execution, query])
        await db.flush()
        step = Step(title=title, slug=f"step-{uuid.uuid4().hex}", status=step_status, data=grid,
                    query_id=str(query.id))
        db.add(step)
        await db.flush()
        viz = Visualization(title=title, status="success", report_id=query_report or t.thread,
                            query_id=str(query.id), view=view)
        db.add(viz)
        await db.flush()
        te = ToolExecution(id=te_id, agent_execution_id=str(execution.id), tool_name=tool_name,
                           arguments_json={"title": title}, status=te_status, success=te_status == "success",
                           created_step_id=str(step.id), artifact_refs_json={"visualizations": [str(viz.id)]})
        db.add(te)
        await db.flush()
        block = CompletionBlock(completion_id=message or t.message, agent_execution_id=str(execution.id),
                                source_type="tool", tool_execution_id=te_id, block_index=block_index, title=title,
                                status="completed")
        db.add(block)
        await db.commit()
        return SimpleNamespace(te=te_id, step=str(step.id), block=str(block.id))


# ── frames (the agent's SSE boundary) ──────────────────────────────────

def _frame(event, data):
    return f"event: {event}\ndata: {json.dumps({'event': event, 'data': data})}\n\n"


def started(t):
    return _frame("completion.started", {"system_completion_id": t.message})


def tool_started(te, name="create_data"):
    return _frame("tool.started", {"tool_name": name, "tool_execution_id": te, "block_id": f"blk-{te}"})


def tool_finished(te, status="success", name="create_data", step=None):
    return _frame("tool.finished", {"tool_name": name, "tool_execution_id": te, "block_id": f"blk-{te}",
                                    "status": status, "created_step_id": step})


def upsert(te, status="success", step=None, name="create_data", title="Sales by genre"):
    return _frame("block.upsert", {"block": {"id": f"blk-{te}", "status": "completed", "tool_execution": {
        "id": te, "tool_name": name, "status": status, "created_step_id": step, "arguments_json": {"title": title}}}})


def finished(status="success"):
    return _frame("completion.finished", {"status": status} if status else {})


def error():
    return _frame("completion.error", {"error": "The model failed"})


def text(token):
    return _frame("block.delta.token", {"block_id": "final", "field": "content", "token": token})


class FakeSSE:
    """Yields ``before``; then waits for ``gate`` (if any); yields ``after``;
    then raises ``fail`` (if any). ``closed`` is set in its own finally."""

    def __init__(self, before, *, gate=None, after=(), fail=None):
        self.before, self.gate, self.after, self.fail = before, gate, after, fail
        self.closed = asyncio.Event()

    async def __call__(self):
        try:
            for f in self.before:
                yield f.encode()
            if self.gate is not None:
                await self.gate.wait()
            for f in self.after:
                yield f
            if self.fail is not None:
                raise self.fail
        finally:
            self.closed.set()


def stream(t, fake):
    return artifact_conversations.ndjson(fake(), thread_id=t.thread, organization_id=t.org)


async def read_to_terminal(gen) -> list[dict]:
    out = []
    async for chunk in gen:
        out.append(json.loads(chunk))
        if out[-1]["type"] in ("completed", "error"):
            break
    return out


async def read_all(gen) -> list[dict]:
    return [json.loads(chunk) async for chunk in gen]


def types(events):
    return [e["type"] for e in events]


def records(caplog, prefix):
    return [r for r in caplog.records if r.name == LOGGER and r.getMessage().startswith(prefix)]


@pytest.fixture(autouse=True)
def _logger_enabled(monkeypatch):
    # The per-test migration run (alembic's fileConfig) disables loggers that
    # already exist; the app never runs it in-process.
    monkeypatch.setattr(logging.getLogger(LOGGER), "disabled", False)


@pytest.fixture
def info_logs(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    return caplog


# ── S18: the deferral is bounded ───────────────────────────────────────

@pytest.mark.asyncio
async def test_completed_is_not_held_past_the_deadline_and_nothing_follows_it(monkeypatch, info_logs):
    monkeypatch.setattr(artifact_conversations, "RESULT_DEFER_SECONDS", 0.05)
    t = await _thread()
    te = str(uuid.uuid4())  # its rows never land in time
    gate = asyncio.Event()
    fake = FakeSSE([started(t), tool_started(te), tool_finished(te), finished()], gate=gate,
                   after=[upsert(te), finished(None)])
    gen = stream(t, fake)
    async with asyncio.timeout(BOUND):
        head = await read_to_terminal(gen)
        assert types(head) == ["started", "completed"] and head[-1]["status"] == "success"
        assert head[-1]["results_incomplete"] is True  # a result was still pending
        gate.set()
        assert await read_all(gen) == []  # the late upsert yields no line at all
    ended = records(info_logs, "ndjson: deferral ended")
    assert len(ended) == 1 and te in ended[0].getMessage()


@pytest.mark.asyncio
async def test_closing_the_stream_after_completed_closes_the_sse(monkeypatch):
    monkeypatch.setattr(artifact_conversations, "RESULT_DEFER_SECONDS", 0.05)
    t = await _thread()
    te = str(uuid.uuid4())
    fake = FakeSSE([started(t), tool_started(te), tool_finished(te), finished()], gate=asyncio.Event())
    gen = stream(t, fake)
    async with asyncio.timeout(BOUND):
        assert types(await read_to_terminal(gen))[-1] == "completed"
    await gen.aclose()
    await asyncio.wait_for(fake.closed.wait(), 5)


# ── S19: no deferral when nothing is pending ───────────────────────────

@pytest.mark.asyncio
async def test_a_result_persisted_before_its_tool_finished_does_not_defer(monkeypatch, info_logs):
    monkeypatch.setattr(artifact_conversations, "RESULT_DEFER_SECONDS", 3600)
    t = await _thread()
    seeded = await _seed(t)
    other = str(uuid.uuid4())
    hold = asyncio.Event()  # never set during the read
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert(seeded.te, step=seeded.step),
                    tool_finished(seeded.te, step=seeded.step), tool_started(other, name="inspect_data"),
                    finished()], gate=hold)
    gen = stream(t, fake)
    try:
        async with asyncio.timeout(BOUND):
            events = await read_to_terminal(gen)
        assert types(events) == ["started", "tool", "result", "completed"]
        assert events[2]["id"] == seeded.step and len(events[2]["rows"]) == 3
        assert events[2]["chart"] == {"type": "bar", "x": "genre", "y": ["sales"], "series": None}
        assert records(info_logs, "ndjson: deferring completed") == []
    finally:
        hold.set()
        await gen.aclose()


# ── S21: the deferral is what delivers a late result ───────────────────

@pytest.mark.asyncio
async def test_completed_waits_for_a_result_persisted_after_the_answer_finished(monkeypatch, info_logs):
    monkeypatch.setattr(artifact_conversations, "RESULT_DEFER_SECONDS", 30)
    t = await _thread()
    te = str(uuid.uuid4())
    gate = asyncio.Event()
    deferring = asyncio.Event()

    class Signal(logging.Handler):
        def emit(self, record):
            if record.getMessage().startswith("ndjson: deferring completed"):
                deferring.set()

    handler = Signal()
    logging.getLogger(LOGGER).addHandler(handler)
    fake = FakeSSE([started(t), tool_started(te), tool_finished(te), finished()], gate=gate,
                   after=[upsert(te), finished(None), finished(None)])
    seen, completed = [], asyncio.Event()

    async def consume():
        async for chunk in stream(t, fake):
            seen.append(json.loads(chunk))
            if seen[-1]["type"] == "completed":
                completed.set()

    consumer = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(BOUND):
            waits = [asyncio.create_task(deferring.wait()), asyncio.create_task(completed.wait())]
            _, rest = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for w in rest:
                w.cancel()
            seeded = await _seed(t, te_id=te)  # the bg persist lands now
            gate.set()
            await consumer
    finally:
        logging.getLogger(LOGGER).removeHandler(handler)
        consumer.cancel()
    assert types(seen) == ["started", "tool", "result", "completed"]
    assert seen[2]["id"] == seeded.step and seen[-1] == {"type": "completed", "status": "success"}
    deferred = records(info_logs, "ndjson: deferring completed")
    ended = records(info_logs, "ndjson: deferral ended")
    assert len(deferred) == 1 and te in deferred[0].getMessage()
    assert len(ended) == 1 and te not in ended[0].getMessage()


# ── S22: terminal lines ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_nothing_follows_an_error():
    t = await _thread()
    te = str(uuid.uuid4())
    fake = FakeSSE([started(t), error(), upsert(te, status="error"), text("late"), finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert types(events) == ["started", "error"]


@pytest.mark.asyncio
async def test_an_error_after_a_finished_error_answer_is_one_completed():
    t = await _thread()
    fake = FakeSSE([started(t), finished("error"), error(), finished(None)])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert types(events) == ["started", "completed"] and events[-1]["status"] == "error"


@pytest.mark.asyncio
async def test_an_error_while_a_result_is_pending_is_ignored(monkeypatch):
    monkeypatch.setattr(artifact_conversations, "RESULT_DEFER_SECONDS", 3600)
    t = await _thread()
    seeded = await _seed(t)
    fake = FakeSSE([started(t), tool_started(seeded.te), tool_finished(seeded.te), finished(), error(),
                    upsert(seeded.te, step=seeded.step), finished(None)])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert types(events) == ["started", "tool", "result", "completed"]
    assert events[-1]["status"] == "success"


# ── S23: a failing SSE read ends the stream ────────────────────────────

@pytest.mark.asyncio
async def test_a_failing_sse_before_the_answer_finished_is_interrupted():
    t = await _thread()
    fake = FakeSSE([started(t)], fail=RuntimeError("socket closed"))
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert types(events) == ["started", "error"] and events[-1]["code"] == "INTERRUPTED"


@pytest.mark.asyncio
async def test_a_failing_sse_while_a_result_is_pending_sweeps_and_completes(monkeypatch, info_logs):
    monkeypatch.setattr(artifact_conversations, "RESULT_DEFER_SECONDS", 3600)
    t = await _thread()
    seeded = await _seed(t)
    fake = FakeSSE([started(t), tool_started(seeded.te), tool_finished(seeded.te), finished()],
                   fail=RuntimeError("socket closed"))
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert types(events) == ["started", "result", "completed"] and events[1]["id"] == seeded.step
    assert any(r.levelno == logging.WARNING for r in info_logs.records if r.name == LOGGER)


# ── S24 and ownership: results_for ─────────────────────────────────────

async def _lookup(t, **filters):
    async with async_session_maker() as s:
        return await results_for(s, thread_id=t.thread, organization_id=t.org, **filters)


@pytest.mark.asyncio
async def test_results_for_needs_a_message_or_tool_filter():
    t = await _thread()
    seeded = await _seed(t)
    assert await _lookup(t) == {}
    assert await _lookup(t, completion_ids=[]) == {}
    assert await _lookup(t, tool_execution_ids=[]) == {}
    assert await _lookup(t, completion_ids=[], tool_execution_ids=[seeded.te]) == {}
    assert await _lookup(t, completion_ids=[t.second_message], tool_execution_ids=[seeded.te]) == {}
    found = await _lookup(t, completion_ids=[t.message], tool_execution_ids=[seeded.te])
    assert list(found) == [t.message] and [r["id"] for r in found[t.message]] == [seeded.step]
    assert (await _lookup(t, tool_execution_ids=[seeded.te]))[t.message][0]["id"] == seeded.step


@pytest.mark.asyncio
async def test_results_for_returns_only_the_threads_own_successful_create_data_steps():
    t = await _thread()
    good = await _seed(t, block_index=5, title="Kept")
    await _seed(t, step_status="error", block_index=1)               # code validation failed
    await _seed(t, te_status="error", block_index=2)                 # the tool failed
    await _seed(t, tool_name="run_query", block_index=3)             # not a result tool (v1)
    await _seed(t, query_report=t.other, block_index=4)              # a step outside the thread
    await _seed(t, execution_report=t.other, block_index=6)          # run outside the thread
    second = await _seed(t, message=t.second_message, block_index=0, title="Second")
    first = await _seed(t, block_index=0, title="First")
    found = await _lookup(t, completion_ids=[t.message, t.second_message])
    assert [r["id"] for r in found[t.message]] == [first.step, good.step]  # block order
    assert [r["id"] for r in found[t.second_message]] == [second.step]
    r = found[t.message][1]
    assert r["block"] == good.block and r["title"] == "Kept" and r["rows"] == ROWS
    assert r["chart"] == {"type": "bar", "x": "genre", "y": ["sales"], "series": None}


# ── H-1: rows committed after the SSE closed ───────────────────────────

class Signal(logging.Handler):
    """Sets ``event`` on the first record starting with ``prefix`` (it runs in
    the translator's task: a deterministic signal, not a sleep)."""

    def __init__(self, prefix):
        super().__init__()
        self.prefix, self.event = prefix, asyncio.Event()

    def emit(self, record):
        if record.getMessage().startswith(self.prefix):
            self.event.set()


@pytest.mark.asyncio
async def test_rows_committed_after_the_sse_closed_still_arrive_before_completed(monkeypatch, info_logs):
    monkeypatch.setattr(artifact_conversations, "RESULT_DEFER_SECONDS", 30)
    t = await _thread()
    te = str(uuid.uuid4())
    polling = Signal("ndjson: the stream ended with results pending")
    logging.getLogger(LOGGER).addHandler(polling)
    # The SSE ends right after the answer finished; the bg persist has not committed.
    fake = FakeSSE([started(t), tool_started(te), tool_finished(te), finished()])
    seen, completed = [], asyncio.Event()

    async def consume():
        async for chunk in stream(t, fake):
            seen.append(json.loads(chunk))
            if seen[-1]["type"] == "completed":
                completed.set()

    consumer = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(BOUND):
            waits = [asyncio.create_task(polling.event.wait()), asyncio.create_task(completed.wait())]
            _, rest = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            for w in rest:
                w.cancel()
            seeded = await _seed(t, te_id=te)  # the bg persist lands now, after the SSE closed
            await consumer
    finally:
        logging.getLogger(LOGGER).removeHandler(polling)
        consumer.cancel()
    assert types(seen) == ["started", "result", "completed"]
    assert seen[1]["id"] == seeded.step and seen[-1] == {"type": "completed", "status": "success"}


# ── results_incomplete: each cause ─────────────────────────────────────

RESULTS_SQL = "FROM completion_blocks JOIN tool_executions"  # the results_for query


def views_sql(sql):
    """The charts' visualization lookup (by query); the timeline's own is by id only."""
    head, _, where = sql.partition(" WHERE ")
    return head.endswith("FROM visualizations") and "visualizations.query_id" in where


class DbFault:
    """A real session whose statements matching ``when`` raise a driver error
    (the database boundary). Counts the statements it is given. ``real``: the
    error comes from the database itself (a failing statement, which aborts a
    PostgreSQL transaction); ``rollback_fails``: so does any rollback."""

    def __init__(self, session, when=lambda sql: False, *, real=False, rollback_fails=False):
        self._session, self._when, self.statements = session, when, []
        self._real, self._rollback_fails = real, rollback_fails

    async def execute(self, stmt, *args, **kwargs):
        sql = " ".join(str(stmt).split())
        self.statements.append(sql)
        if self._when(sql):
            if self._real:
                await self._session.execute(sql_text("SELECT missing_column FROM no_such_table_here"))
            raise OperationalError(sql[:80], {}, ConnectionError("server closed the connection unexpectedly"))
        return await self._session.execute(stmt, *args, **kwargs)

    async def rollback(self):
        if self._rollback_fails:
            raise OperationalError("ROLLBACK", {}, ConnectionError("server closed the connection unexpectedly"))
        return await self._session.rollback()

    def __getattr__(self, name):
        return getattr(self._session, name)


def faulty_sessions(monkeypatch, when):
    real = app.dependencies.async_session_maker

    @contextlib.asynccontextmanager
    async def maker():
        async with real() as session:
            yield DbFault(session, when)

    monkeypatch.setattr(app.dependencies, "async_session_maker", maker)


@pytest.mark.asyncio
async def test_a_failed_lookup_marks_the_results_incomplete_and_the_answer_still_flows(monkeypatch):
    t = await _thread()
    seeded = await _seed(t)
    faulty_sessions(monkeypatch, lambda sql: RESULTS_SQL in sql)
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert(seeded.te, step=seeded.step),
                    tool_finished(seeded.te, step=seeded.step), text("Rock leads."), finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert types(events) == ["started", "tool", "text_delta", "completed"]
    assert events[-1] == {"type": "completed", "status": "success", "results_incomplete": True}


def _wide(n_cols, field_chars, rows):
    cols = [f"c{i}" + "f" * field_chars for i in range(n_cols)]
    return {"columns": [{"field": c, "headerName": f"c{i}"} for i, c in enumerate(cols)],
            "rows": [{c: "v" for c in cols} for _ in range(rows)], "info": {"total_rows": rows}}


BIG = {"rows": [{"t": f"{i:05d}" + "q" * 1495, "u": f"{i:05d}" + "r" * 1495} for i in range(200)],
       "columns": [{"field": "t", "headerName": "t"}, {"field": "u", "headerName": "u"}],
       "info": {"total_rows": 200}}
LARGE_KEYS = _wide(30, 5000, 1)  # ~150 KiB even with no rows: never fits what two BIG ones leave


@pytest.mark.asyncio
async def test_a_result_lost_to_the_message_budget_marks_the_results_incomplete():
    t = await _thread()
    first = await _seed(t, grid=BIG, view=None, block_index=0, title="One")
    second = await _seed(t, grid=BIG, view=None, block_index=1, title="Two")
    await _seed(t, grid=LARGE_KEYS, view=None, block_index=2, title="Three")
    fake = FakeSSE([started(t), finished()])  # no tool frames: the sweep finds all three
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert [e["id"] for e in events if e["type"] == "result"] == [first.step, second.step]
    assert events[-1]["results_incomplete"] is True


@pytest.mark.asyncio
async def test_a_complete_answer_carries_no_incomplete_flag():
    t = await _thread()
    seeded = await _seed(t)
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert(seeded.te, step=seeded.step),
                    tool_finished(seeded.te, step=seeded.step), finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert events[-1] == {"type": "completed", "status": "success"}


# ── PII settings that cannot be read: withhold, never leak ─────────────

def _broken(_config):
    raise RuntimeError("ruleset failed to compile")


@pytest.fixture
def loader_clock(monkeypatch):
    """The PII loader's clock (its cache and failure backoff); asyncio's is untouched."""
    now = [time.monotonic()]
    monkeypatch.setattr(pii_loader, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


@pytest.fixture
def pii_unreadable(monkeypatch):
    """Licensed, the org's PII protection on, and its ruleset failing to load."""
    monkeypatch.setattr(pii_loader, "has_feature", lambda feature: True)
    monkeypatch.setattr(pii_loader, "build_redactor", _broken)
    pii_loader.invalidate()
    yield monkeypatch
    pii_loader.invalidate()


async def _pii_on(t):
    async with async_session_maker() as db:
        db.add(OrganizationSettings(organization_id=t.org,
                                    config={"pii_protection": {"enabled": True, "mode": "replace"}}))
        await db.commit()


def _email():
    return f"ann.{uuid.uuid4().hex[:8]}@example.com"


@pytest.mark.asyncio
async def test_results_and_tool_titles_are_withheld_when_the_pii_settings_cannot_be_read(pii_unreadable):
    t = await _thread()
    await _pii_on(t)
    a, b = _email(), _email()
    grid = {**GRID, "rows": [{"genre": f"Rock by {a}", "sales": 1.5}], "info": {**GRID["info"], "total_rows": 1}}
    seeded = await _seed(t, grid=grid, title=f"Sales for {a}")
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert(seeded.te, step=seeded.step, title=f"Ask {b}"),
                    tool_finished(seeded.te, step=seeded.step), text("Rock leads."), finished()])
    async with asyncio.timeout(BOUND):
        raw = [chunk async for chunk in stream(t, fake)]
    body = b"".join(raw).decode()
    assert a not in body and b not in body
    events = [json.loads(c) for c in raw]
    assert types(events) == ["started", "tool", "text_delta", "completed"]
    assert events[1]["title"] is None and events[2]["text"] == "Rock leads."
    assert events[-1]["results_incomplete"] is True


@pytest.mark.asyncio
async def test_a_failed_redactor_load_is_retried_for_later_tool_titles(pii_unreadable, loader_clock):
    t = await _thread()
    await _pii_on(t)
    a, b = _email(), _email()
    one, two = str(uuid.uuid4()), str(uuid.uuid4())
    gate = asyncio.Event()
    fake = FakeSSE([started(t), upsert(one, name="inspect_data", title=f"Ask {a}")], gate=gate,
                   after=[upsert(two, name="inspect_data", title=f"Ask {b}"), finished()])
    gen = stream(t, fake)
    async with asyncio.timeout(BOUND):
        head = [json.loads(await gen.__anext__()) for _ in range(2)]
        assert types(head) == ["started", "tool"] and head[1]["title"] is None
        pii_unreadable.setattr(pii_loader, "build_redactor", build_redactor)  # the settings read recovers
        loader_clock[0] += 2  # past the loader's failure backoff
        gate.set()
        rest = await read_all(gen)
    assert types(rest) == ["tool", "completed"]
    assert rest[0]["title"] and b not in rest[0]["title"] and rest[0]["title"].startswith("Ask ")


# ── L-1: a pump failure that is not forwarded is still logged ──────────

class Abort(BaseException):
    """Not an Exception: the pump does not forward it."""


@pytest.mark.asyncio
async def test_a_pump_failure_outside_exception_is_logged(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    t = await _thread()
    fake = FakeSSE([started(t)], fail=Abort("worker shutting down"))
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert types(events) == ["started", "error"]
    logged = [r for r in caplog.records if r.name == LOGGER and r.levelno >= logging.WARNING and r.exc_info]
    assert any(isinstance(r.exc_info[1], Abort) for r in logged)


# ── history: results_incomplete, resilience, one visualization query ──

async def _history(t, db):
    from app.models.organization import Organization as Org
    from app.models.report import Report as Rep
    from app.models.user import User as Usr

    thread, org, user = await db.get(Rep, t.thread), await db.get(Org, t.org), await db.get(Usr, t.user)
    out = await artifact_conversations.messages(db, thread, org, user, limit=30, before=None)
    return {m["id"]: m for m in out["items"] if m["role"] == "assistant"}


@pytest.mark.asyncio
async def test_history_survives_a_failed_results_lookup():
    t = await _thread(status="success")
    await _seed(t)
    async with async_session_maker() as s:
        history = await _history(t, DbFault(s, lambda sql: RESULTS_SQL in sql))
    assert set(history) == {t.message, t.second_message}
    for m in history.values():
        assert m["results"] == [] and m["results_incomplete"] is True


@pytest.mark.asyncio
async def test_a_failed_visualization_lookup_keeps_every_table_without_charts():
    t = await _thread(status="success")
    one = await _seed(t, block_index=0, title="One")
    two = await _seed(t, block_index=1, title="Two")
    async with async_session_maker() as s:
        history = await _history(t, DbFault(s, views_sql))
    m = history[t.message]
    assert [r["id"] for r in m["results"]] == [one.step, two.step]
    assert all(r["rows"] == ROWS and r["chart"] is None for r in m["results"])
    assert m["results_incomplete"] is True
    assert "results_incomplete" not in history[t.second_message]  # it had no results to lose


@pytest.mark.asyncio
async def test_history_marks_results_lost_to_the_budget_or_unshapeable():
    t = await _thread(status="success")
    await _seed(t, grid=BIG, view=None, block_index=0, title="One")
    await _seed(t, grid=BIG, view=None, block_index=1, title="Two")
    await _seed(t, grid=LARGE_KEYS, view=None, block_index=2, title="Three")
    kept = await _seed(t, message=t.second_message, block_index=0, title="Kept")
    await _seed(t, message=t.second_message, grid=_wide(30, 9000, 0), view=None, block_index=1, title="Too wide")
    async with async_session_maker() as s:
        history = await _history(t, s)
    assert [r["title"] for r in history[t.message]["results"]] == ["One", "Two"]
    assert history[t.message]["results_incomplete"] is True
    assert [r["id"] for r in history[t.second_message]["results"]] == [kept.step]
    assert history[t.second_message]["results_incomplete"] is True


@pytest.mark.asyncio
async def test_history_withholds_results_and_tool_titles_when_the_pii_settings_cannot_be_read(pii_unreadable):
    t = await _thread(status="success")
    await _pii_on(t)
    a = _email()
    await _seed(t, grid={**GRID, "rows": [{"genre": f"Rock by {a}", "sales": 1.5}],
                         "info": {**GRID["info"], "total_rows": 1}}, title=f"Sales for {a}")
    async with async_session_maker() as s:
        history = await _history(t, s)
    assert a not in json.dumps(history, default=str)
    m = history[t.message]
    assert m["results"] == [] and m["results_incomplete"] is True
    assert m["tools"] and all(tool["title"] is None for tool in m["tools"])


@pytest.mark.asyncio
async def test_visualizations_are_loaded_in_one_query_whatever_the_number_of_results():
    t = await _thread()
    await _seed(t, message=t.second_message, block_index=0)
    for i in range(3):
        await _seed(t, block_index=i, title=f"R{i}")
    counts = {}
    for message in (t.second_message, t.message):
        async with async_session_maker() as s:
            db = DbFault(s)
            found = await results_for(db, thread_id=t.thread, organization_id=t.org, completion_ids=[message])
        assert all(r["chart"] == {"type": "bar", "x": "genre", "y": ["sales"], "series": None}
                   for r in found[message])
        counts[len(found[message])] = len(db.statements)
    assert set(counts) == {1, 3} and counts[1] == counts[3]


# ── results_incomplete is decided by the final state ───────────────────

def once(match):
    """``match``, true only the first time it matches."""
    fired = []

    def when(sql):
        if match(sql) and not fired:
            fired.append(sql)
            return True
        return False

    return when


@pytest.mark.asyncio
async def test_a_lookup_that_fails_once_and_is_recovered_by_the_sweep_is_complete(monkeypatch):
    t = await _thread()
    seeded = await _seed(t)
    faulty_sessions(monkeypatch, once(lambda sql: RESULTS_SQL in sql))  # the tool's own lookup fails
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert(seeded.te, step=seeded.step),
                    tool_finished(seeded.te, step=seeded.step), text("Rock leads."), finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert types(events) == ["started", "tool", "text_delta", "result", "completed"]
    assert events[3]["id"] == seeded.step and events[3]["rows"] == ROWS
    assert events[-1] == {"type": "completed", "status": "success"}


@pytest.mark.asyncio
async def test_a_chart_lost_to_a_transient_visualization_failure_is_restored_by_the_sweep(monkeypatch):
    t = await _thread()
    seeded = await _seed(t)
    faulty_sessions(monkeypatch, once(views_sql))
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert(seeded.te, step=seeded.step),
                    tool_finished(seeded.te, step=seeded.step), finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    results = [e for e in events if e["type"] == "result"]
    assert [r["id"] for r in results] == [seeded.step]
    assert results[0]["chart"] == {"type": "bar", "x": "genre", "y": ["sales"], "series": None}
    assert events[-1] == {"type": "completed", "status": "success"}


@pytest.mark.asyncio
async def test_a_pii_load_that_recovers_before_the_sweep_delivers_the_result_complete(
        monkeypatch, pii_unreadable, loader_clock):
    t = await _thread()
    await _pii_on(t)
    seeded = await _seed(t)
    looked = asyncio.Event()

    def when(sql):  # the tool's own lookup reached the database: its redaction was decided (withheld)
        if RESULTS_SQL in sql:
            looked.set()
        return False

    faulty_sessions(monkeypatch, when)
    gate = asyncio.Event()
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert(seeded.te, step=seeded.step),
                    tool_finished(seeded.te, step=seeded.step)], gate=gate, after=[finished()])
    seen = []

    async def consume():
        async for chunk in stream(t, fake):
            seen.append(json.loads(chunk))

    consumer = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(BOUND):
            await looked.wait()
            pii_unreadable.setattr(pii_loader, "build_redactor", build_redactor)  # the settings read recovers
            loader_clock[0] += 2  # past the loader's failure backoff
            gate.set()
            await consumer
    finally:
        consumer.cancel()
    assert types(seen) == ["started", "tool", "result", "completed"]
    assert seen[1]["title"] is None  # withheld while the settings could not be read
    assert seen[2]["id"] == seeded.step
    assert seen[-1] == {"type": "completed", "status": "success"}


@pytest.mark.parametrize("step_status", ["running", "error"])
@pytest.mark.asyncio
async def test_a_successful_create_data_whose_step_was_not_stored_is_incomplete_in_stream_and_history(step_status):
    # The agent reports the tool as success, but its step was not stored as a
    # success (a swallowed save error, a failed validation): no table can come.
    t = await _thread(status="success")
    seeded = await _seed(t, step_status=step_status)
    fake = FakeSSE([started(t), tool_started(seeded.te), tool_finished(seeded.te, step=seeded.step),
                    upsert(seeded.te, step=seeded.step), text("Rock leads."), finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert types(events) == ["started", "tool", "text_delta", "completed"]
    assert events[-1] == {"type": "completed", "status": "success", "results_incomplete": True}
    async with async_session_maker() as s:
        history = await _history(t, s)
    assert history[t.message]["results"] == [] and history[t.message]["results_incomplete"] is True
    assert "results_incomplete" not in history[t.second_message]  # it ran no result tool


@pytest.mark.asyncio
async def test_history_leaves_its_session_usable_after_a_failed_visualization_lookup():
    t = await _thread(status="success")
    one = await _seed(t)
    async with async_session_maker() as s:
        history = await _history(t, DbFault(s, views_sql, real=True))
        # The request goes on with the same session (PostgreSQL aborts the transaction on an error).
        assert (await s.execute(select(Report.id).where(Report.id == t.thread))).scalar_one() == t.thread
    assert [r["id"] for r in history[t.message]["results"]] == [one.step]
    assert history[t.message]["results_incomplete"] is True


@pytest.mark.asyncio
async def test_a_failed_rollback_after_a_failed_history_lookup_is_logged(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    t = await _thread(status="success")
    await _seed(t)
    async with async_session_maker() as s:
        history = await _history(t, DbFault(s, lambda sql: RESULTS_SQL in sql, rollback_fails=True))
    assert history[t.message]["results"] == [] and history[t.message]["results_incomplete"] is True
    failed = [r for r in caplog.records if r.name == LOGGER and r.exc_info
              and isinstance(r.exc_info[1], OperationalError) and "ROLLBACK" in str(r.exc_info[1])]
    assert len(failed) == 1 and t.thread in failed[0].getMessage()


# ── teardown: an outer cancellation is never swallowed ─────────────────

class SlowCloseSSE(FakeSSE):
    """Yields ``before``, then waits forever; closing it takes until ``release``."""

    def __init__(self, before):
        super().__init__(before)
        self.closing, self.release = asyncio.Event(), asyncio.Event()

    async def __call__(self):
        try:
            for f in self.before:
                yield f.encode()
            await asyncio.Event().wait()
        finally:
            self.closing.set()
            await self.release.wait()


@pytest.mark.asyncio
async def test_a_cancellation_while_the_stream_closes_is_not_swallowed():
    t = await _thread()
    fake = SlowCloseSSE([started(t)])
    gen = stream(t, fake)

    async def consume():
        assert json.loads(await gen.__anext__())["type"] == "started"
        await gen.aclose()  # the response closes the stream (client gone)

    consumer = asyncio.create_task(consume())
    try:
        async with asyncio.timeout(BOUND):
            await fake.closing.wait()  # the teardown is waiting for the SSE to close
            consumer.cancel()          # and the request is cancelled meanwhile
            await asyncio.wait([consumer])
    finally:
        fake.release.set()
    assert consumer.cancelled()


# ── failure logs name what failed ──────────────────────────────────────

@pytest.mark.asyncio
async def test_stream_failure_logs_name_the_message(caplog):
    caplog.set_level(logging.WARNING, logger=LOGGER)
    t = await _thread()
    async with asyncio.timeout(BOUND):
        await read_all(stream(t, FakeSSE([started(t)], fail=RuntimeError("socket closed"))))
        await read_all(stream(t, FakeSSE([started(t)], fail=Abort("worker shutting down"))))
    failed = [r for r in caplog.records if r.name == LOGGER and r.exc_info]
    by_cause = {type(r.exc_info[1]): r.getMessage() for r in failed}
    assert set(by_cause) == {RuntimeError, Abort}
    assert all(t.message in m for m in by_cause.values())


# ── results_incomplete is accounting, never a lookup outcome ───────────

def nth(match, n):
    """``match``, true only the ``n``-th time it matches."""
    hits = []

    def when(sql):
        if match(sql):
            hits.append(sql)
            return len(hits) == n
        return False

    return when


def upsert_in(block, te, step=None, title="Sales by genre"):
    """A result tool's final block under its stored id (the agent reuses the id it streamed)."""
    return _frame("block.upsert", {"block": {"id": block, "status": "completed", "tool_execution": {
        "id": te, "tool_name": "create_data", "status": "success", "created_step_id": step,
        "arguments_json": {"title": title}}}})


@pytest.mark.parametrize("sweep_fault", ["results", "views"])
@pytest.mark.asyncio
async def test_a_failed_final_sweep_after_every_result_was_sent_whole_is_complete(monkeypatch, sweep_fault):
    t = await _thread()
    seeded = await _seed(t)
    # Only the second statement of the kind fails: the final sweep's (the tool's own lookup worked).
    faulty_sessions(monkeypatch, nth((lambda sql: RESULTS_SQL in sql) if sweep_fault == "results" else views_sql, 2))
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert(seeded.te, step=seeded.step),
                    tool_finished(seeded.te, step=seeded.step), finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    results = [e for e in events if e["type"] == "result"]
    assert [r["id"] for r in results] == [seeded.step] and results[0]["chart"] is not None
    assert events[-1] == {"type": "completed", "status": "success"}


@pytest.mark.asyncio
async def test_a_pending_result_delivered_by_the_final_sweep_is_complete(monkeypatch):
    monkeypatch.setattr(artifact_conversations, "RESULT_DEFER_SECONDS", 0.3)
    t = await _thread()
    seeded = await _seed(t)
    # Every lookup of the tool itself fails; the final sweep (by message) works.
    faulty_sessions(monkeypatch, lambda sql: RESULTS_SQL in sql and "tool_executions.id IN" in sql)
    fake = FakeSSE([started(t), tool_started(seeded.te), tool_finished(seeded.te, step=seeded.step), finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert [e["id"] for e in events if e["type"] == "result"] == [seeded.step]
    assert events[-1] == {"type": "completed", "status": "success"}


@pytest.mark.asyncio
async def test_a_block_naming_no_step_after_a_failed_lookup_is_resolved_by_the_tool_finished_step(monkeypatch):
    t = await _thread()
    seeded = await _seed(t)
    faulty_sessions(monkeypatch, once(lambda sql: RESULTS_SQL in sql))  # the tool's own lookup fails
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert(seeded.te),  # the block names no step
                    tool_finished(seeded.te, step=seeded.step), text("Rock leads."), finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert [e["id"] for e in events if e["type"] == "result"] == [seeded.step]
    assert events[-1] == {"type": "completed", "status": "success"}


@pytest.mark.asyncio
async def test_a_tool_that_never_names_its_step_is_resolved_by_the_sweeps_block(monkeypatch):
    t = await _thread()
    seeded = await _seed(t)
    faulty_sessions(monkeypatch, once(lambda sql: RESULTS_SQL in sql))  # the tool's own lookup fails
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert_in(seeded.block, seeded.te),
                    tool_finished(seeded.te), finished()])  # neither frame names the step
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    assert [e["id"] for e in events if e["type"] == "result"] == [seeded.step]
    assert events[-1] == {"type": "completed", "status": "success"}


@pytest.mark.asyncio
async def test_a_result_first_delivered_by_a_lossy_sweep_is_incomplete(monkeypatch):
    # Guard: the sweep is the first to send it, and without its chart (the views failed): not whole.
    t = await _thread()
    seeded = await _seed(t)
    faulty_sessions(monkeypatch, lambda sql: (RESULTS_SQL in sql and "tool_executions.id IN" in sql) or views_sql(sql))
    fake = FakeSSE([started(t), tool_started(seeded.te), upsert(seeded.te, step=seeded.step),
                    tool_finished(seeded.te, step=seeded.step), finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    results = [e for e in events if e["type"] == "result"]
    assert [r["id"] for r in results] == [seeded.step] and results[0]["chart"] is None
    assert events[-1]["results_incomplete"] is True


# ── the message budget: the stream and history cut the same table ──────

@pytest.mark.asyncio
async def test_the_stream_and_history_apply_the_budget_in_the_same_order():
    # A parallel batch: the third block's tool finished before the second's.
    # The stream fits results as they arrive (as each tool finishes); history
    # must keep the same tables and say the same about completeness.
    from datetime import datetime, timedelta

    t = await _thread()
    one = await _seed(t, grid=BIG, view=None, block_index=0, title="One")
    two = await _seed(t, grid=BIG, view=None, block_index=1, title="Two")
    three = await _seed(t, grid=LARGE_KEYS, view=None, block_index=2, title="Three")
    base = datetime(2026, 10, 29, 12, 0, 0)
    async with async_session_maker() as db:
        for seeded, seconds in ((one, 0), (three, 1), (two, 2)):  # completion order: one, three, two
            (await db.get(ToolExecution, seeded.te)).completed_at = base + timedelta(seconds=seconds)
        await db.commit()
    fake = FakeSSE([started(t)] + [upsert(s.te, step=s.step) for s in (one, three, two)] + [finished()])
    async with asyncio.timeout(BOUND):
        events = await read_all(stream(t, fake))
    streamed = [e for e in events if e["type"] == "result"]
    assert [r["id"] for r in streamed] == [one.step, three.step, two.step]
    assert streamed[2]["rows"] and streamed[2]["truncated"] is True  # cut to what was left, not lost
    assert events[-1] == {"type": "completed", "status": "success"}
    async with async_session_maker() as s:
        m = (await _history(t, s))[t.message]
    assert [r["id"] for r in m["results"]] == [one.step, three.step, two.step]
    assert [len(r["rows"]) for r in m["results"]] == [len(r["rows"]) for r in streamed]
    assert "results_incomplete" not in m


# ── logs name the thread and the message; repeated failures are quiet ──

@pytest.mark.asyncio
async def test_deferral_and_polling_logs_name_the_thread_and_the_message(monkeypatch, info_logs):
    monkeypatch.setattr(artifact_conversations, "RESULT_DEFER_SECONDS", 0.05)
    t = await _thread()
    te = str(uuid.uuid4())  # never lands
    fake = FakeSSE([started(t), tool_started(te), tool_finished(te), finished()])  # then the SSE closes
    async with asyncio.timeout(BOUND):
        await read_all(stream(t, fake))
    for prefix in ("ndjson: deferring completed", "ndjson: deferral ended",
                   "ndjson: the stream ended with results pending"):
        (record,) = records(info_logs, prefix)
        assert t.thread in record.getMessage() and t.message in record.getMessage(), prefix


@pytest.mark.asyncio
async def test_a_failed_visualization_lookup_log_names_the_thread_and_the_messages(monkeypatch, caplog):
    results_logger = "app.services.artifact_conversation_results"
    monkeypatch.setattr(logging.getLogger(results_logger), "disabled", False)  # see _logger_enabled
    caplog.set_level(logging.WARNING, logger=results_logger)
    t = await _thread()
    await _seed(t)
    async with async_session_maker() as s:
        await results_for(DbFault(s, views_sql, rollback_fails=True), thread_id=t.thread, organization_id=t.org,
                          completion_ids=[t.message])
    logged = [r.getMessage() for r in caplog.records if r.name == results_logger]
    views = [m for m in logged if "visualization lookup failed" in m]
    rollback = [m for m in logged if "rollback after a database error failed" in m]
    assert len(views) == 1 and t.thread in views[0] and t.message in views[0]
    assert len(rollback) == 1 and t.thread in rollback[0] and t.message in rollback[0]


@pytest.mark.asyncio
async def test_a_tool_lookup_that_keeps_failing_warns_once_then_logs_at_debug(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    monkeypatch.setattr(artifact_conversations, "RESULT_DEFER_SECONDS", 0.3)
    monkeypatch.setattr(artifact_conversations, "RESULT_POLL_SECONDS", 0.02)
    t = await _thread()
    seeded = await _seed(t)
    faulty_sessions(monkeypatch, lambda sql: RESULTS_SQL in sql and "tool_executions.id IN" in sql)
    fake = FakeSSE([started(t), tool_started(seeded.te), tool_finished(seeded.te, step=seeded.step), finished()])
    async with asyncio.timeout(BOUND):
        await read_all(stream(t, fake))  # the SSE closed: the tool is looked for every poll
    failed = [r for r in caplog.records if r.name == LOGGER and r.getMessage().startswith("ndjson: result lookup failed")
              and seeded.te in r.getMessage()]
    assert len(failed) >= 3
    assert [r.levelno for r in failed] == [logging.WARNING] + [logging.DEBUG] * (len(failed) - 1)
    assert failed[0].exc_info is not None


@pytest.mark.asyncio
async def test_reasoning_stream_and_history_stay_separate_from_answer():
    t = await _thread(status="success")
    seeded = await _seed(t)
    # Only the agent writes completion blocks; seed its persisted output here.
    async with async_session_maker() as db:
        block = await db.get(CompletionBlock, seeded.block)
        block.reasoning = "Check the totals."
        block.content = "The total is 42."
        await db.commit()
        saved = (await _history(t, db))[t.message]
    assert saved["thinking"] == "Check the totals."
    assert saved["text"] == "The total is 42."
    events = await read_all(stream(t, FakeSSE([
        started(t),
        _frame("block.delta.token", {"block_id": "a", "field": "reasoning", "token": "Check "}),
        text("The total is 42."),
        _frame("block.delta.text", {"block_id": "a", "field": "reasoning", "text": "Check the totals."}),
        _frame("block.delta.token", {"block_id": "b", "field": "reasoning", "token": "Done."}),
        _frame("block.delta.text", {"block_id": "b", "field": "reasoning", "text": ""}),
        finished(),
    ])))
    assert [e for e in events if e["type"].startswith("thinking")] == [
        {"type": "thinking_delta", "block": "a", "text": "Check "},
        {"type": "thinking", "block": "a", "text": "Check the totals."},
        {"type": "thinking_delta", "block": "b", "text": "Done."},
        {"type": "thinking", "block": "b", "text": ""},
    ]
    assert [e["text"] for e in events if e["type"] == "text_delta"] == ["The total is 42."]
    assert events[-1]["type"] == "completed"
