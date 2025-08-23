from typing import AsyncIterator, Dict, Any, Optional, Type
from pydantic import BaseModel


class Tool:
    name: str = ""
    description: str = ""
    input_model: Optional[Type[BaseModel]] = None
    output_model: Optional[Type[BaseModel]] = None

    async def run_stream(self, tool_input: Dict[str, Any], runtime_ctx: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """Stream tool lifecycle events.

        Yields dict events like:
        {"type": "tool.start", "payload": {...}}
        {"type": "tool.progress", "payload": {...}}
        {"type": "tool.partial", "payload": {...}}
        {"type": "tool.stdout", "payload": str}
        {"type": "tool.end", "payload": {"output": {...}, "observation": {...}}}
        {"type": "tool.error", "payload": {"message": str}}
        """
        yield {"type": "tool.error", "payload": {"message": "Not implemented"}}

