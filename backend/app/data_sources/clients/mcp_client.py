import logging
from typing import List, Dict, Any, Optional
from app.data_sources.clients.tool_provider_base import ToolProviderClient

logger = logging.getLogger(__name__)


class McpClient(ToolProviderClient):
    """
    Client for connecting to MCP (Model Context Protocol) servers.
    Supports SSE and Streamable HTTP transports (stdio planned for later).
    Uses the `mcp` Python SDK for protocol handling.
    """

    def __init__(
        self,
        server_url: str,
        transport: str = "sse",
        command: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        headers: Optional[Dict[str, str]] = None,
        token: Optional[str] = None,
        api_key: Optional[str] = None,
        api_key_header: Optional[str] = None,
    ):
        self.server_url = server_url
        self.transport = transport
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.headers = headers or {}
        self.token = token
        self.api_key = api_key
        self.api_key_header = api_key_header or "X-API-Key"

    def _build_headers(self) -> Dict[str, str]:
        """Build request headers, merging any auth headers."""
        h = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.api_key:
            h[self.api_key_header] = self.api_key
        h.update(self.headers)
        return h

    def list_tools(self) -> List[Dict[str, Any]]:
        """
        Connect to the MCP server and retrieve the list of available tools.
        Uses the mcp SDK's ClientSession for protocol handling.
        """
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self._alist_tools())

    async def _alist_tools(self) -> List[Dict[str, Any]]:
        """Async implementation of list_tools using the MCP SDK."""
        try:
            tools = []
            async with self._connect() as session:
                result = await session.list_tools()
                for tool in result.tools:
                    tools.append({
                        "name": tool.name,
                        "description": tool.description or "",
                        "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                        "output_schema": {},
                    })
            return tools
        except BaseException as e:
            raise RuntimeError(self._unwrap_exception(e)) from None

    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool on the MCP server."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(
            self._acall_tool(tool_name, arguments)
        )

    async def _acall_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """Async implementation of call_tool using the MCP SDK."""
        from mcp import ClientSession

        try:
            async with self._connect() as session:
                result = await session.call_tool(tool_name, arguments)

                # Determine content type from result
                data = self._extract_result_data(result)
                content_type = self._detect_content_type(data)

                return {
                    "success": not getattr(result, "isError", False),
                    "data": data,
                    "content_type": content_type,
                    "error": None,
                }
        except BaseException as e:
            msg = self._unwrap_exception(e)
            logger.error(f"MCP tool call failed: {tool_name}: {msg}")
            return {
                "success": False,
                "data": None,
                "content_type": "text",
                "error": msg,
            }

    def _extract_result_data(self, result) -> Any:
        """Extract usable data from an MCP CallToolResult."""
        if not hasattr(result, "content") or not result.content:
            return None

        # If single text content, return as string
        if len(result.content) == 1:
            content = result.content[0]
            if hasattr(content, "text"):
                # Try to parse as JSON
                import json
                try:
                    return json.loads(content.text)
                except (json.JSONDecodeError, TypeError):
                    return content.text

        # Multiple content blocks — return as list
        parts = []
        for content in result.content:
            if hasattr(content, "text"):
                import json
                try:
                    parts.append(json.loads(content.text))
                except (json.JSONDecodeError, TypeError):
                    parts.append(content.text)
            elif hasattr(content, "data"):
                parts.append(content.data)
        return parts

    @staticmethod
    def _unwrap_exception(e: BaseException) -> str:
        """Extract the root cause message from ExceptionGroup or nested exceptions."""
        if isinstance(e, BaseExceptionGroup):
            for exc in e.exceptions:
                return McpClient._unwrap_exception(exc)
        return str(e)

    def _detect_content_type(self, data: Any) -> str:
        """Detect whether data is tabular, text, or generic JSON."""
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            return "tabular"
        if isinstance(data, str):
            return "text"
        return "json"

    def _connect(self):
        """
        Create an MCP client session context manager for the configured transport.
        Returns an async context manager that yields a ClientSession.
        """
        from mcp import ClientSession
        from mcp.client.sse import sse_client
        from mcp.client.streamable_http import streamablehttp_client
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _session():
            if self.transport == "sse":
                async with sse_client(
                    url=self.server_url,
                    headers=self._build_headers(),
                ) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        yield session

            elif self.transport == "streamable_http":
                async with streamablehttp_client(
                    url=self.server_url,
                    headers=self._build_headers(),
                ) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        yield session

            else:
                raise ValueError(
                    f"Unsupported MCP transport: {self.transport}. "
                    "Supported: 'sse', 'streamable_http'"
                )

        return _session()

    def test_connection(self) -> Dict[str, Any]:
        """Test connectivity by attempting to initialize and list tools."""
        import asyncio
        return asyncio.get_event_loop().run_until_complete(self._atest_connection())

    async def _atest_connection(self) -> Dict[str, Any]:
        try:
            async with self._connect() as session:
                result = await session.list_tools()
                tool_count = len(result.tools) if result.tools else 0
                return {
                    "success": True,
                    "message": f"Connected to MCP server. {tool_count} tool(s) available.",
                }
        except BaseException as e:
            return {
                "success": False,
                "message": f"Failed to connect to MCP server: {self._unwrap_exception(e)}",
            }

    # Override async wrappers to use native async implementations
    async def alist_tools(self) -> List[Dict[str, Any]]:
        return await self._alist_tools()

    async def acall_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return await self._acall_tool(tool_name, arguments)

    async def atest_connection(self) -> Dict[str, Any]:
        return await self._atest_connection()
