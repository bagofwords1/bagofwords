"""MCP API routes - Model Context Protocol implementation.

Provides a JSON-RPC 2.0 endpoint for Claude, Cursor, and other MCP-compatible clients.
Based on the MCP Streamable HTTP transport specification.

Authentication:
  - API key: Authorization: Bearer bow_<key> or X-API-Key header (Claude Code, Cursor)
  - OAuth 2.1: Authorization: Bearer bow_oauth_<token> (Claude Web)
Organization is derived from the API key or OAuth token.
"""

import asyncio
import json
import logging
import os
import re
from typing import Any, Optional, Union, Tuple

from fastapi import APIRouter, Depends, Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.core.auth import current_user, _jwt_current_user, api_key_header
from app.dependencies import get_async_db, require_mcp_enabled, get_current_organization
from app.models.user import User
from app.models.organization import Organization
from app.ai.tools.mcp import get_mcp_tool, list_mcp_tools
from app.settings.config import settings

logger = logging.getLogger(__name__)

MCP_PROTOCOL_VERSION = "2025-11-25"


def _resource_metadata_url(request: Request) -> str:
    """Build the well-known URL for the WWW-Authenticate header."""
    configured = settings.bow_config.base_url
    if configured and configured not in ("http://0.0.0.0:3000", "http://0.0.0.0:8000"):
        base = configured.rstrip("/")
    else:
        base = f"{request.url.scheme}://{request.url.netloc}"
    return f"{base}/.well-known/oauth-protected-resource"


async def mcp_auth(
    request: Request,
    jwt_user: Optional[User] = Depends(_jwt_current_user),
    api_key: Optional[str] = Depends(api_key_header),
    db: AsyncSession = Depends(get_async_db),
) -> Tuple[User, Organization]:
    """Authenticate MCP requests via JWT, API key, or OAuth access token.

    Returns (user, organization). On failure, raises 401 with WWW-Authenticate
    header pointing to the OAuth protected resource metadata.
    """
    # 1. Try JWT
    if jwt_user is not None:
        try:
            org = await get_current_organization(request, db)
            return jwt_user, org
        except HTTPException:
            pass

    # 2. Try API key from X-API-Key header
    if api_key and api_key.startswith("bow_") and not api_key.startswith("bow_oauth_"):
        from app.services.api_key_service import ApiKeyService
        svc = ApiKeyService()
        user = await svc.get_user_by_api_key(db, api_key)
        if user:
            org = await svc.get_organization_by_api_key(db, api_key)
            if org:
                return user, org

    # 3. Try Bearer token from Authorization header
    auth_header = request.headers.get("Authorization", "")
    token_was_provided = False
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        token_was_provided = bool(token)

        # 3a. OAuth access token
        if token.startswith("bow_oauth_"):
            from app.services.oauth_server_service import OAuthServerService
            svc = OAuthServerService()
            result = await svc.validate_access_token(db, token)
            if result:
                return result  # (user, organization)
            logger.warning("OAuth token validation failed for token starting with: %s...", token[:16])

        # 3b. API key via Bearer
        if token.startswith("bow_") and not token.startswith("bow_oauth_"):
            from app.services.api_key_service import ApiKeyService
            svc = ApiKeyService()
            user = await svc.get_user_by_api_key(db, token)
            if user:
                org = await svc.get_organization_by_api_key(db, token)
                if org:
                    return user, org

    # No valid auth — return 401 with OAuth metadata
    resource_url = _resource_metadata_url(request)
    # Include error="invalid_token" when a token was provided but failed validation
    # so the client knows to refresh rather than re-authorize from scratch (RFC 6750 §3.1)
    if token_was_provided:
        www_auth = f'Bearer resource_metadata="{resource_url}", error="invalid_token", error_description="The access token is invalid or expired"'
    else:
        www_auth = f'Bearer resource_metadata="{resource_url}"'
    raise HTTPException(
        status_code=401,
        detail="Not authenticated",
        headers={
            "WWW-Authenticate": www_auth,
        },
    )


def _check_mcp_enabled(organization: Organization):
    """Raise 403 if MCP is not enabled for the organization."""
    if not organization.settings:
        raise HTTPException(status_code=403, detail="MCP integration is not enabled for this organization")
    mcp_config = organization.settings.get_config("mcp_enabled")
    if not mcp_config or not getattr(mcp_config, "value", False):
        raise HTTPException(status_code=403, detail="MCP integration is not enabled for this organization")


def _mcp_response(content: Any, status_code: int = 200) -> JSONResponse:
    """Create a JSONResponse with MCP-Protocol-Version header."""
    response = JSONResponse(content, status_code=status_code)
    response.headers["MCP-Protocol-Version"] = MCP_PROTOCOL_VERSION
    return response


router = APIRouter(tags=["mcp"])


class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    id: Optional[Union[str, int]] = None
    method: str
    params: Optional[dict] = None


def jsonrpc_response(id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": id, "result": result}


def jsonrpc_error(id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}}


MCP_CAPABILITIES = {
    "tools": {},
    "resources": {},
}

# UI resource definitions for MCP Apps
MCP_UI_RESOURCES = [
    {
        "uri": "ui://bagofwords/visualization",
        "name": "Visualization Viewer",
        "mimeType": "text/html;profile=mcp-app",
    },
    {
        "uri": "ui://bagofwords/artifact",
        "name": "Artifact Viewer",
        "mimeType": "text/html;profile=mcp-app",
    },
]

# Cache for loaded HTML bundles
_html_bundle_cache: dict[str, str] = {}


def _load_html_bundle(name: str) -> str:
    """Load an MCP App HTML bundle from the frontend/public directory.

    MCP app HTML is delivered as a string and rendered in a sandboxed iframe
    (srcdoc / blob:), so external script URLs won't resolve.  This function
    inlines any ``<script src="/libs/...">`` references by reading the vendored
    JS file and replacing the tag with an inline ``<script>`` block.
    """
    if name in _html_bundle_cache:
        return _html_bundle_cache[name]

    # Try multiple paths: built output first, then source
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", ".output", "public", f"{name}.html"),
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "public", f"{name}.html"),
    ]

    content: str | None = None
    html_dir: str | None = None
    for path in candidates:
        resolved = os.path.normpath(path)
        if os.path.isfile(resolved):
            with open(resolved, "r", encoding="utf-8") as f:
                content = f.read()
            html_dir = os.path.dirname(resolved)
            break

    if content is None:
        return f"<html><body>MCP App bundle '{name}' not found.</body></html>"

    # Inline /libs/ script references so the bundle is fully self-contained
    def _inline_script(match: re.Match) -> str:
        attrs = match.group(1)
        src_match = re.search(r'src="(/libs/[^"]+)"', attrs)
        if not src_match:
            return match.group(0)
        rel_path = src_match.group(1).lstrip("/")
        # Resolve relative to the HTML file's parent (frontend/public/)
        lib_path = os.path.normpath(os.path.join(html_dir, rel_path))
        if not os.path.isfile(lib_path):
            logger.warning("MCP bundle '%s': vendored lib not found at %s", name, lib_path)
            return match.group(0)  # keep original tag as fallback
        with open(lib_path, "r", encoding="utf-8") as lf:
            js_content = lf.read()
        return f"<script>{js_content}</script>"

    content = re.sub(r"<script\b([^>]*\bsrc=\"/libs/[^\"]+\"[^>]*)>\s*</script>", _inline_script, content)

    _html_bundle_cache[name] = content
    return content


@router.get("/mcp")
async def mcp_get_endpoint(
    auth: Tuple[User, Organization] = Depends(mcp_auth),
):
    """MCP GET endpoint - returns server info."""
    _, organization = auth
    _check_mcp_enabled(organization)
    return _mcp_response({
        "jsonrpc": "2.0",
        "result": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {
                "name": "bagofwords",
                "version": "1.0.0",
            },
            "capabilities": MCP_CAPABILITIES,
        }
    })


@router.post("/mcp")
async def mcp_endpoint(
    raw_request: Request,
    auth: Tuple[User, Organization] = Depends(mcp_auth),
    db: AsyncSession = Depends(get_async_db),
):
    """MCP JSON-RPC endpoint.

    Handles:
    - initialize: MCP initialization handshake
    - tools/list: List available tools
    - tools/call: Execute a tool
    """
    user, organization = auth
    _check_mcp_enabled(organization)

    # Parse raw body
    try:
        body = await raw_request.json()
        logger.info(f"MCP request body: {body}")
    except Exception as e:
        logger.error(f"Failed to parse MCP request: {e}")
        return _mcp_response(jsonrpc_error(None, -32700, f"Parse error: {str(e)}"))

    try:
        request = JsonRpcRequest(**body)
    except Exception as e:
        logger.error(f"Invalid JSON-RPC request: {e}")
        return _mcp_response(jsonrpc_error(None, -32600, f"Invalid request: {str(e)}"))

    if request.method == "initialize":
        return _mcp_response(jsonrpc_response(request.id, {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "serverInfo": {
                "name": "bagofwords",
                "version": "1.0.0",
            },
            "capabilities": MCP_CAPABILITIES,
        }))

    elif request.method == "tools/list":
        # Include all tools (model + app-only) so MCP Apps can call app-only tools
        tools = list_mcp_tools(include_app_only=True)
        mcp_tools = []
        for tool in tools:
            entry: dict[str, Any] = {
                "name": tool["name"],
                "description": tool["description"],
                "inputSchema": tool["input_schema"],
            }
            if "_meta" in tool:
                entry["_meta"] = tool["_meta"]
            mcp_tools.append(entry)
        return _mcp_response(jsonrpc_response(request.id, {"tools": mcp_tools}))

    elif request.method == "tools/call":
        params = request.params or {}
        tool_name = params.get("name")
        arguments = params.get("arguments", {})

        if not tool_name:
            return _mcp_response(jsonrpc_error(request.id, -32602, "Missing tool name"))

        tool_class = get_mcp_tool(tool_name)
        if not tool_class:
            return _mcp_response(jsonrpc_error(request.id, -32602, f"Unknown tool: {tool_name}"))

        # Check if client supports SSE streaming
        accept_header = raw_request.headers.get("accept", "")
        use_sse = "text/event-stream" in accept_header
        logger.info(f"MCP tools/call Accept header: '{accept_header}', use_sse={use_sse}")

        tool = tool_class()

        if not use_sse:
            # Non-streaming fallback: return plain JSON as before
            try:
                result = await tool.execute(arguments, db, user, organization)
                return _mcp_response(jsonrpc_response(request.id, {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False,
                }))
            except Exception as e:
                logger.exception(f"Tool execution error: {e}")
                return _mcp_response(jsonrpc_response(request.id, {
                    "content": [{"type": "text", "text": str(e)}],
                    "isError": True,
                }))

        # SSE streaming: send progress heartbeats every 5s while tool executes
        async def _sse_tool_stream():
            tool_task = asyncio.create_task(tool.execute(arguments, db, user, organization))

            try:
                while not tool_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(tool_task), timeout=5.0)
                    except asyncio.TimeoutError:
                        # Tool still running — send SSE comment to keep connection alive.
                        # Comments are ignored by all SSE clients per spec, so this
                        # won't be mistaken for a tool result by any MCP client.
                        yield ": keepalive\n\n"

                # Tool finished — get result
                result = tool_task.result()
                final_response = jsonrpc_response(request.id, {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                    "isError": False,
                })
                yield f"event: message\ndata: {json.dumps(final_response)}\n\n"

            except Exception as e:
                logger.exception(f"Tool execution error (SSE): {e}")
                if not tool_task.done():
                    tool_task.cancel()
                error_response = jsonrpc_response(request.id, {
                    "content": [{"type": "text", "text": str(e)}],
                    "isError": True,
                })
                yield f"event: message\ndata: {json.dumps(error_response)}\n\n"

        return StreamingResponse(
            _sse_tool_stream(),
            media_type="text/event-stream",
            headers={"MCP-Protocol-Version": MCP_PROTOCOL_VERSION, "Cache-Control": "no-cache"},
        )

    elif request.method == "resources/list":
        return _mcp_response(jsonrpc_response(request.id, {
            "resources": MCP_UI_RESOURCES,
        }))

    elif request.method == "resources/read":
        params = request.params or {}
        uri = params.get("uri", "")

        # Map UI resource URIs to HTML bundle filenames
        bundle_map = {
            "ui://bagofwords/visualization": "mcp-visualization-app",
            "ui://bagofwords/artifact": "mcp-artifact-app",
        }

        bundle_name = bundle_map.get(uri)
        if not bundle_name:
            return _mcp_response(jsonrpc_error(
                request.id, -32602, f"Unknown resource URI: {uri}"
            ))

        html_content = _load_html_bundle(bundle_name)
        return _mcp_response(jsonrpc_response(request.id, {
            "contents": [{
                "uri": uri,
                "mimeType": "text/html;profile=mcp-app",
                "text": html_content,
            }],
        }))

    else:
        return _mcp_response(jsonrpc_error(request.id, -32601, f"Method not found: {request.method}"))


# REST endpoint for testing/debugging
@router.get("/mcp/tools")
async def get_tools_rest(
    auth: Tuple[User, Organization] = Depends(mcp_auth),
):
    """REST endpoint to list MCP tools (for testing/debugging)."""
    _, organization = auth
    _check_mcp_enabled(organization)
    return {"tools": list_mcp_tools()}
