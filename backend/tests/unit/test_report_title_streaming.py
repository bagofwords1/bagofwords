"""Report titles are generated from the prompt and streamed, not written at
the end of the run.

What the product promises:

* A report stops being "untitled report" seconds after the user hits send —
  the title comes from the prompt itself, so it never waits on the planner,
  the tools, or the run finishing.
* The generated title reaches open clients over the completion stream
  (``report.title.updated``), which is what lets the report header and the
  sidebar update mid-run.
* Generation never overwrites a title someone (or an earlier turn) already
  set. That matters much more now that the write lands while the user is
  looking at the report and may rename it in the same second.

The tests drive the agent's title path with a stubbed reporter (the LLM is a
boundary) against a real DB, and deliberately leave the agent's context/planner
attributes unset: if titling ever starts depending on built context again, these
tests fail with AttributeError instead of silently regressing to end-of-run
timing.
"""
import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.ai.agent_v2 import AgentV2
from app.dependencies import async_session_maker
from app.models.organization import Organization
from app.models.report import Report
from app.models.user import User


class FakeQueue:
    """Stand-in for the per-completion SSE queue."""

    def __init__(self):
        self.events = []

    async def put(self, event):
        self.events.append(event)


class FakeReporter:
    """Boundary stub for the small-model title call."""

    def __init__(self, title="Quarterly Revenue Breakdown", before_return=None):
        self.title = title
        self.seen = []
        self._before_return = before_return

    async def generate_report_title(self, messages, plan=None):
        self.seen.append({"messages": messages, "plan": plan})
        if self._before_return is not None:
            await self._before_return()
        return self.title


async def _seed_report(title: str) -> str:
    """An org + one report carrying `title`. Returns the report id."""
    suffix = uuid.uuid4().hex[:8]
    async with async_session_maker() as db:
        org = Organization(name=f"Title Org {suffix}")
        user = User(name="Title Tester", email=f"titles-{suffix}@example.com", hashed_password="x")
        db.add_all([org, user])
        await db.flush()
        report = Report(
            title=title,
            slug=f"report-{suffix}",
            organization_id=org.id,
            user_id=user.id,
        )
        db.add(report)
        await db.commit()
        return str(report.id)


async def _read_title(report_id: str) -> str:
    async with async_session_maker() as db:
        row = (await db.execute(select(Report.title).where(Report.id == report_id))).scalar_one()
        return row


def _agent(report_id: str, current_title: str, reporter: FakeReporter, queue: FakeQueue) -> AgentV2:
    """Bare agent carrying only what the title path is allowed to need."""
    agent = AgentV2.__new__(AgentV2)
    agent._title_task = None
    agent._session_maker = async_session_maker
    agent.reporter = reporter
    agent.event_queue = queue
    agent.system_completion_id = str(uuid.uuid4())
    agent.current_execution = None
    # The agent's in-memory view of the report: only the title is read, and
    # the id is captured as a string before any await.
    agent.report = SimpleNamespace(id=report_id, title=current_title)
    agent.report_id = report_id
    agent.head_completion = SimpleNamespace(prompt={"content": "irrelevant"})
    return agent


async def _run_title(agent: AgentV2, prompt: str):
    agent._start_title_generation(prompt)
    await agent._await_title_generation()


def _title_events(queue: FakeQueue):
    return [e for e in queue.events if e.event == "report.title.updated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", ["", "untitled report", "  Untitled Report  "])
async def test_untitled_report_is_titled_from_the_prompt_and_streamed(placeholder):
    """Any placeholder title is replaced, and the new title is streamed.

    The prompt is the only input: no plan, no built context, no finished run.
    """
    report_id = await _seed_report(placeholder)
    reporter = FakeReporter(title="Quarterly Revenue Breakdown")
    queue = FakeQueue()
    agent = _agent(report_id, placeholder, reporter, queue)

    prompt = "Break down quarterly revenue by region for FY24"
    await _run_title(agent, prompt)

    assert await _read_title(report_id) == "Quarterly Revenue Breakdown"
    # The model was asked to title the user's request, not a plan.
    assert prompt in reporter.seen[0]["messages"]
    assert not reporter.seen[0]["plan"]

    events = _title_events(queue)
    assert len(events) == 1
    assert events[0].data["report_id"] == report_id
    assert events[0].data["title"] == "Quarterly Revenue Breakdown"


@pytest.mark.asyncio
async def test_a_report_that_already_has_a_title_is_left_alone():
    """A real title — a rename, or one an earlier turn generated — wins, and
    nothing is streamed to clients that would overwrite what they show."""
    report_id = await _seed_report("Warehouse Reconciliation")
    reporter = FakeReporter(title="Something Else Entirely")
    queue = FakeQueue()
    agent = _agent(report_id, "Warehouse Reconciliation", reporter, queue)

    await _run_title(agent, "Reconcile inventory between the warehouse and the system")

    assert await _read_title(report_id) == "Warehouse Reconciliation"
    assert reporter.seen == []
    assert _title_events(queue) == []


@pytest.mark.asyncio
async def test_a_rename_during_generation_is_not_clobbered():
    """The user renames the report while the title call is in flight.

    Generation starts against a placeholder, so the in-memory gate passes; the
    persisted write must still lose to the rename, and no event may claim the
    generated title.
    """
    report_id = await _seed_report("untitled report")

    async def rename():
        async with async_session_maker() as db:
            report = (await db.execute(select(Report).where(Report.id == report_id))).scalar_one()
            report.title = "Renamed By Hand"
            await db.commit()

    reporter = FakeReporter(title="Generated Too Late", before_return=rename)
    queue = FakeQueue()
    agent = _agent(report_id, "untitled report", reporter, queue)

    await _run_title(agent, "anything at all")

    assert await _read_title(report_id) == "Renamed By Hand"
    assert _title_events(queue) == []


@pytest.mark.asyncio
async def test_an_empty_prompt_does_not_burn_a_title_call():
    report_id = await _seed_report("untitled report")
    reporter = FakeReporter()
    queue = FakeQueue()
    agent = _agent(report_id, "untitled report", reporter, queue)

    await _run_title(agent, "   ")

    assert await _read_title(report_id) == "untitled report"
    assert reporter.seen == []


@pytest.mark.asyncio
async def test_a_failed_title_call_leaves_the_turn_and_the_report_intact():
    """Titling is best-effort: an LLM failure must not raise into the run, and
    the placeholder must survive so a later turn can retry."""
    report_id = await _seed_report("untitled report")

    class BoomReporter:
        async def generate_report_title(self, messages, plan=None):
            raise RuntimeError("provider exploded")

    queue = FakeQueue()
    agent = _agent(report_id, "untitled report", BoomReporter(), queue)

    await _run_title(agent, "Show me revenue by country")

    assert await _read_title(report_id) == "untitled report"
    assert _title_events(queue) == []


@pytest.mark.asyncio
async def test_titling_runs_alongside_the_turn_rather_than_after_it():
    """The title is available while the run is still going.

    Kicking generation off returns immediately and the title lands without the
    caller ever finishing (or even starting) the planner loop.
    """
    report_id = await _seed_report("untitled report")
    release = asyncio.Event()

    async def wait_for_release():
        await release.wait()

    reporter = FakeReporter(title="Churned Customers", before_return=wait_for_release)
    queue = FakeQueue()
    agent = _agent(report_id, "untitled report", reporter, queue)

    agent._start_title_generation("Which customers churned last quarter")
    # Kickoff does not block the caller: the turn proceeds while the small
    # model is still thinking.
    assert await _read_title(report_id) == "untitled report"

    release.set()
    await agent._await_title_generation()

    assert await _read_title(report_id) == "Churned Customers"
    assert len(_title_events(queue)) == 1


@pytest.mark.asyncio
async def test_reporter_prompt_omits_the_plan_when_there_is_none():
    """Titling at prompt time has no plan to show; the prompt must not carry a
    dangling, empty plan section (and must still use one when given)."""
    from app.ai.agents.reporter.reporter import Reporter

    captured = []

    reporter = Reporter.__new__(Reporter)
    reporter.organization_settings = None
    reporter.llm = SimpleNamespace(
        inference=lambda text, **kw: captured.append(text) or "A Title"
    )

    await reporter.generate_report_title("Show me revenue by country")
    assert "Show me revenue by country" in captured[0]
    assert "this plan" not in captured[0]

    await reporter.generate_report_title("Show me revenue by country", [{"action": "create_widget"}])
    assert "this plan" in captured[1]
    assert "create_widget" in captured[1]
