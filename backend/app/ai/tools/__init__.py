"""Top-level tools package exports.

- Exports Tool interfaces and metadata
- Auto-imports all implementations so callers can access them without per-tool edits
"""

from .base import Tool
from .metadata import ToolMetadata
from .schemas import *  # re-export schemas
from .utils import format_tool_schemas, format_tool_catalog_for_prompt, get_tool_by_name

# Auto-import implementations so their classes are available via app.ai.tools.implementations
from . import implementations as _implementations  # noqa: F401

__all__ = [
    "Tool",
    "ToolMetadata",
    "format_tool_schemas",
    "format_tool_catalog_for_prompt",
    "get_tool_by_name",
]