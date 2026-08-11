from collections.abc import AsyncIterator
from typing import Any

from app.ai.registry import ToolRegistry
from app.ai.tools.base import Tool
from app.ai.tools.metadata import ToolMetadata


def _empty_registry() -> ToolRegistry:
    registry = ToolRegistry.__new__(ToolRegistry)
    registry._factories = {}
    registry._metadata_cache = {}
    return registry


def test_register_reuses_class_cached_tool_metadata() -> None:
    class CountingTool(Tool):
        metadata_builds = 0

        @property
        def metadata(self) -> ToolMetadata:
            type(self).metadata_builds += 1
            return ToolMetadata(name="counting", description="Counts schema builds")

        async def run_stream(
            self, tool_input: dict[str, Any], runtime_ctx: dict[str, Any]
        ) -> AsyncIterator[Any]:
            if False:
                yield None

    Tool._META_CACHE.pop(CountingTool, None)
    try:
        _empty_registry().register(CountingTool)
        _empty_registry().register(CountingTool)
        assert CountingTool.metadata_builds == 1
    finally:
        Tool._META_CACHE.pop(CountingTool, None)
