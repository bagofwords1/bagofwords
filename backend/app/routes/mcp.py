"""MCP API routes - Model Context Protocol implementation.

Provides a JSON-RPC 2.0 endpoint for Claude, Cursor, and other MCP-compatible clients.
Based on the MCP Streamable HTTP transport specification.

Authentication via Authorization: Bearer <api_key> or X-API-Key header.
Organization is derived from the API key.
"""

import json
import logging
from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel
from typing import Any, Optional, Union

from app.core.auth import current_user
from app.dependencies import get_async_db, require_mcp_enabled
from app.models.user import User
from app.models.organization import Organization
from app.ai.tools.mcp import get_mcp_tool, list_mcp_tools

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["mcp"],
    dependencies=[Depends(require_mcp_enabled)]
)


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    method: str
    params: Optional[dict] = None


def jsonrpc_response(id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def jsonrpc_error(id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


@router.get("/mcp")
async def mcp_get_endpoint(
    user: User = Depends(current_user),
):
    """MCP GET endpoint - returns server info."""
    return JSONResponse({
        "jsonrpc": "2.0",
        "result": {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": "bagofwords",
                "version": "1.0.0",
            },
            "capabilities": {
                "tools": {},
            },
        }
    })


@router.post("/mcp")
async def mcp_endpoint(
    raw_request: Request,
    user: User = Depends(current_user),
    organization: Organization = Depends(require_mcp_enabled),
    db: AsyncSession = Depends(get_async_db),
):
    """MCP JSON-RPC endpoint.
    
    Handles:
    - initialize: MCP initialization handshake
    - tools/list: List available tools
    - tools/call: Execute a tool
    """
    # Parse raw body
    try:
        body = await raw_request.json()
        logger.info(f"MCP request body: {body}")
    except Exception as e:
        logger.error(f"Failed to parse MCP request: {e}")
        return JSONResponse(jsonrpc_error(None, -32700, f"Parse error: {str(e)}"))
    
    try:
        request = JsonRpcRequest(**body)
    except Exception as e:
        logger.error(f"Invalid JSON-RPC request: {e}")
        return JSONResponse(jsonrpc_error(None, -32600, f"Invalid request: {str(e)}"))
    
    if request.method == "initialize":
        # MCP initialization handshake
        return JSONResponse(jsonrpc_response(request.id, {
            "protocolVersion": "2024-11-05",
            "serverInfo": {
                "name": "bagofwords",
                "version": "1.0.0",
            },
            "capabilities": {
                "tools": {},
            },
        }))
    
    elif request.method == "tools/list":
        tools = list_mcp_tools()
        # Convert to MCP format (inputSchema instead of input_schema)
        mcp_tools = []
        for tool in tools:
            mcp_tools.append({
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": tool["input_schema"],
            })
        return JSONResponse(jsonrpc_response(request.id, {"tools": mcp_tools}))
    
    elif request.method == "tools/call":
        params = request.params or {}
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        
        if not tool_name:
            return JSONResponse(jsonrpc_error(request.id, -32602, "Missing tool name"))
        
        tool_class = get_mcp_tool(tool_name)
        if not tool_class:
            return JSONResponse(jsonrpc_error(request.id, -32602, f"Unknown tool: {tool_name}"))
        
        tool = tool_class()
        try:
            result = await tool.execute(arguments, db, user, organization)
            # MCP expects content array with type/text - use JSON for proper serialization
            return JSONResponse(jsonrpc_response(request.id, {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": False,
            }))
        except Exception as e:
            logger.exception(f"Tool execution error: {e}")
            return JSONResponse(jsonrpc_response(request.id, {
                "content": [{"type": "text", "text": str(e)}],
                "isError": True,
            }))
    
    else:
        return JSONResponse(jsonrpc_error(request.id, -32601, f"Method not found: {request.method}"))


# REST endpoint for testing/debugging
@router.get("/mcp/tools")
async def get_tools_rest(
    user: User = Depends(current_user),
):
    """REST endpoint to list MCP tools (for testing/debugging)."""
    return {"tools": list_mcp_tools()}
