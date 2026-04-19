"""Pending-result registry for the write_officejs_code tool.

The tool emits an excel_action on tool.partial, then awaits a Future keyed by
tool_call_id. The taskpane posts the result back via POST /completions/.../tool-results/{id},
which resolves the Future so the tool can emit tool.end.

Single-process only. If multi-node deployment ever happens, move to Redis pubsub.
"""

import asyncio
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class PendingOfficeJsRegistry:
    def __init__(self) -> None:
        self._futures: Dict[str, asyncio.Future] = {}

    def register(self, tool_call_id: str) -> asyncio.Future:
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._futures[tool_call_id] = fut
        return fut

    def resolve(self, tool_call_id: str, result: Dict[str, Any]) -> bool:
        fut = self._futures.get(tool_call_id)
        if not fut or fut.done():
            return False
        try:
            fut.set_result(result)
        except Exception as e:
            logger.warning("Failed to resolve officejs future %s: %s", tool_call_id, e)
            return False
        return True

    def forget(self, tool_call_id: str) -> None:
        self._futures.pop(tool_call_id, None)

    def has(self, tool_call_id: str) -> bool:
        return tool_call_id in self._futures


pending_officejs_registry = PendingOfficeJsRegistry()
