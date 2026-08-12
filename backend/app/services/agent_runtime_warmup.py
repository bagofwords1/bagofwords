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

    logger.info(
        "Agent runtime warmed in %.0fms (tools=%d, connector_types=%d/%d)",
        (time.perf_counter() - started_at) * 1000,
        len(registry.list_tools()),
        len(warmed_types),
        len(connection_types),
    )
