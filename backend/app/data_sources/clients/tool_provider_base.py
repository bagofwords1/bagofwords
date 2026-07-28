import asyncio
from abc import ABC, abstractmethod
from typing import List, Dict, Any


class ToolProviderClient(ABC):
    """
    Abstract base class for tool-providing connections (MCP servers, custom APIs).
    Parallel to DataSourceClient but for tool discovery and invocation
    instead of schema discovery and SQL execution.
    """

    @abstractmethod
    def list_tools(self) -> List[Dict[str, Any]]:
        """
        Discover available tools from the provider.

        Returns a list of tool definitions:
        [
            {
                "name": "tool_name",
                "description": "What this tool does",
                "input_schema": {...},   # JSON Schema for parameters
                "output_schema": {...},  # JSON Schema for output (optional)
            },
            ...
        ]
        """
        pass

    @abstractmethod
    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute a tool and return the result.

        Returns:
            {
                "success": bool,
                "data": Any,           # The tool's output
                "content_type": str,   # "tabular" | "text" | "json" | "binary"
                "error": str | None,   # Error message if failed
            }
        """
        pass

    @abstractmethod
    def test_connection(self) -> Dict[str, Any]:
        """
        Test connectivity to the tool provider.

        Returns:
            {
                "success": bool,
                "message": str,
            }
        """
        pass

    # Async wrappers — offload blocking I/O to a thread so the event loop stays free.

    async def alist_tools(self) -> List[Dict[str, Any]]:
        return await asyncio.to_thread(self.list_tools)

    async def acall_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await asyncio.to_thread(self.call_tool, tool_name, arguments)

    async def atest_connection(self) -> Dict[str, Any]:
        return await asyncio.to_thread(self.test_connection)

    # Resources — optional capability. Only MCP servers expose resources;
    # providers that don't (e.g. custom API) inherit these defaults so callers
    # get a clear NotImplementedError instead of an AttributeError.

    async def alist_resources(self) -> List[Dict[str, Any]]:
        raise NotImplementedError("This provider does not support resources")

    async def alist_resource_templates(self) -> List[Dict[str, Any]]:
        raise NotImplementedError("This provider does not support resources")

    async def aread_resource(self, uri: str) -> Dict[str, Any]:
        raise NotImplementedError("This provider does not support resources")


def codegen_clients(clients: Dict[str, Any] | None) -> Dict[str, Any]:
    """Drop tool-provider clients from a `ds_clients` dict.

    Generated code receives `ds_clients` and is told every entry answers
    `execute_query`. A tool provider answers no such thing — it is reached
    through execute_mcp, which builds its own client over the connection's
    wire. Advertising one to the coder invites it to fetch data itself, and the
    call it invents (`ds_clients["Agent:Conn"].execute_mcp(...)`) exists on no
    client, so the whole code attempt fails. Filter at every point where the
    dict crosses into codegen.
    """
    return {
        key: client
        for key, client in (clients or {}).items()
        if not isinstance(client, ToolProviderClient)
    }
