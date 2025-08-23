from abc import ABC, abstractmethod
from typing import AsyncIterator, Dict, Any, Optional, Type
from pydantic import BaseModel

from .metadata import ToolMetadata


class Tool(ABC):
    """Base interface for all tools.

    Tools must implement run_stream and declare metadata property.
    """

    @property
    @abstractmethod
    def metadata(self) -> ToolMetadata:
        """Tool metadata for registry and discovery."""
        pass

    @property
    def name(self) -> str:
        """Tool name from metadata."""
        return self.metadata.name

    @property
    def description(self) -> str:
        """Tool description from metadata."""
        return self.metadata.description

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

