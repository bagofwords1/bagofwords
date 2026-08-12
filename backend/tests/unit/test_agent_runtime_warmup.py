from unittest.mock import MagicMock

import pytest

from app.services import agent_runtime_warmup


class _AsyncSessionContext:
    def __init__(self, session: MagicMock) -> None:
        self.session = session

    async def __aenter__(self) -> MagicMock:
        return self.session

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_warm_agent_runtime_preloads_tools_and_active_connector_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = MagicMock()
    registry.list_tools.return_value = [object(), object()]
    monkeypatch.setattr(agent_runtime_warmup, "ToolRegistry", lambda: registry)

    result = MagicMock()
    result.scalars.return_value.all.return_value = ["postgres", "snowflake"]
    session = MagicMock()

    async def execute(*args: object, **kwargs: object) -> MagicMock:
        return result

    session.execute = execute
    monkeypatch.setattr(
        agent_runtime_warmup,
        "async_session_maker",
        lambda: _AsyncSessionContext(session),
    )

    resolved: list[str] = []
    monkeypatch.setattr(
        agent_runtime_warmup,
        "resolve_client_class",
        lambda connection_type: resolved.append(connection_type),
    )

    await agent_runtime_warmup.warm_agent_runtime()

    assert resolved == ["postgres", "snowflake"]
    registry.list_tools.assert_called_once_with()
