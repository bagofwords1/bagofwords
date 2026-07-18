from abc import ABC, abstractmethod
from typing import AsyncIterator, ClassVar, Dict, Any, Optional, Type
from pydantic import BaseModel

from .metadata import ToolMetadata


class Tool(ABC):
    """Base interface for all tools.

    Tools must implement run_stream and declare metadata property.
    """

    # Per-class cache of the built ToolMetadata. Each subclass's `metadata`
    # property rebuilds a ToolMetadata — including two model_json_schema()
    # calls — on every access, and a fresh tool instance is created for every
    # tool call (registry.get() -> tool_class()). Since registry tools are
    # constructed with no args, their metadata is class-invariant, so we build
    # it once per class. Callers on the hot path (tool_runner, name/description)
    # use `spec`; `metadata` stays uncached for anything that needs it live.
    _META_CACHE: ClassVar[Dict[type, ToolMetadata]] = {}

    @property
    @abstractmethod
    def metadata(self) -> ToolMetadata:
        """Tool metadata for registry and discovery."""
        pass

    @property
    def spec(self) -> ToolMetadata:
        """Class-cached tool metadata (avoids per-call schema regeneration).

        Safe for all registry-constructed tools, whose metadata depends only on
        class-level constants. A tool whose metadata varies per instance must
        override this to return ``self.metadata``.
        """
        cls = type(self)
        cached = Tool._META_CACHE.get(cls)
        if cached is None:
            cached = self.metadata
            Tool._META_CACHE[cls] = cached
        return cached

    @property
    def name(self) -> str:
        """Tool name from metadata."""
        return self.spec.name

    @property
    def description(self) -> str:
        """Tool description from metadata."""
        return self.spec.description

    @property
    def input_model(self) -> Optional[Type[BaseModel]]:
        """Override in subclass to provide input validation."""
        return None

    @property
    def output_model(self) -> Optional[Type[BaseModel]]:
        """Override in subclass to provide output validation."""
        return None

    @abstractmethod
    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator['ToolEvent']:
        """Stream tool execution events.

        Args:
            tool_input: validated input arguments
            runtime_ctx: runtime context (db, org, completion, etc.)

        Yields:
            ToolEvent: typed streaming events (ToolStart, ToolProgress, etc.)
        """
        pass

