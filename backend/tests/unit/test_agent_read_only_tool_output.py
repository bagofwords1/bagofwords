"""Read-only tool finalization must remain free of database write work."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.ai.agent_v2 import AgentV2


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["read_query", "read_artifact"])
async def test_successful_read_only_tool_output_skips_write_session(tool_name):
    agent = object.__new__(AgentV2)
    agent.current_execution = SimpleNamespace(id="execution-id")
    agent.report = SimpleNamespace(id="report-id")
    agent.system_completion = SimpleNamespace(id="system-completion-id")
    agent.head_completion = SimpleNamespace(user_id="user-id")

    opened_sessions = 0
    fetched_models: list[str] = []

    class RecordingSession:
        async def get(self, model, _identity):
            fetched_models.append(model.__name__)
            return SimpleNamespace(id=_identity)

    @asynccontextmanager
    async def recording_write_session():
        nonlocal opened_sessions
        opened_sessions += 1
        yield RecordingSession()

    agent._writes_session = recording_write_session
    invocation = SimpleNamespace(
        current_step_id=None,
        current_visualization=None,
    )

    observation = {"success": True, "summary": "Requested data was read"}
    original_observation = dict(observation)
    await agent._handle_tool_output(
        tool_name,
        {},
        observation,
        tool_output={"success": True},
        inv=invocation,
    )

    assert observation == original_observation
    assert opened_sessions == 0, "a read-only tool opened the agent's write session"
    assert not fetched_models, (
        "a read-only tool fetched stateful ORM rows during no-op finalization: "
        f"{fetched_models}"
    )
