"""Warm process-local agent dependencies before the first prompt arrives."""

import time

from sqlalchemy import select

from app.ai.registry import ToolRegistry
from app.dependencies import async_session_maker
from app.models.connection import Connection
from app.schemas.data_source_registry import resolve_client_class
from app.settings.logging_config import get_logger


logger = get_logger(__name__)


async def warm_agent_runtime() -> None:
    """Populate tool metadata and active connector imports for this worker.

    Agent construction is process-local, so every web worker otherwise makes
    its first user prompt pay for tool discovery/schema generation and lazy
    imports of the connector clients used by that deployment.
    """
    started_at = time.perf_counter()
    registry = ToolRegistry()

    async with async_session_maker() as session:
        result = await session.execute(
            select(Connection.type)
            .where(Connection.is_active.is_(True))
            .distinct()
        )
        connection_types = tuple(result.scalars().all())

    warmed_types: list[str] = []
    for connection_type in connection_types:
        try:
            resolve_client_class(connection_type)
            warmed_types.append(connection_type)
        except Exception:
            # A broken optional connector must not keep the whole application
            # from starting; the normal request path will still surface its
            # actionable import error if that connector is used.
            logger.exception(
                "Failed to pre-import connector client type=%s",
                connection_type,
            )

    await _warm_llm_sdks()

    logger.info(
        "Agent runtime warmed in %.0fms (tools=%d, connector_types=%d/%d)",
        (time.perf_counter() - started_at) * 1000,
        len(registry.list_tools()),
        len(warmed_types),
        len(connection_types),
    )


async def _warm_llm_sdks() -> None:
    """Run each provider SDK's request preparation once, offline.

    The SDKs import their resource modules and build their type-driven
    request transforms lazily, so the first model call of a worker paid for
    it (~300-450 ms measured for OpenAI chat: ``client.chat`` imports
    ``openai.resources``). Nothing is sent: only imports and the transform
    run, on a tiny request.
    """
    for module in ("openai.resources", "anthropic.resources"):
        try:
            __import__(module)
        except Exception:
            logger.debug("%s warmup import skipped", module, exc_info=True)
    tool = {"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {}}}
    try:
        from anthropic._utils._transform import async_maybe_transform as a_transform
        from anthropic.types import message_create_params

        await a_transform(
            {"model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}],
             "system": [{"type": "text", "text": "s"}], "tools": [tool], "stream": True},
            message_create_params.MessageCreateParamsStreaming,
        )
    except Exception:
        logger.debug("anthropic SDK warmup skipped", exc_info=True)
    try:
        from openai._utils._transform import async_maybe_transform as o_transform
        from openai.types.chat import completion_create_params
        from openai.types.responses import response_create_params

        fn = {"type": "function", "function": {"name": "t", "description": "d", "parameters": {"type": "object"}}}
        await o_transform(
            {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": [fn], "stream": True},
            completion_create_params.CompletionCreateParamsStreaming,
        )
        await o_transform(
            {"model": "m", "input": "hi", "stream": True,
             "tools": [{"type": "function", "name": "t", "parameters": {"type": "object"}, "strict": False}]},
            response_create_params.ResponseCreateParamsStreaming,
        )
    except Exception:
        logger.debug("openai SDK warmup skipped", exc_info=True)
