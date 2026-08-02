"""A streamable-HTTP MCP server that requires a bearer token — a stand-in for
monday.com's hosted MCP server.

It reproduces the exact failure a per-user OAuth (DCR) connector hits when
something calls it with no user in context: the request goes out with no
Authorization header and the server answers

    HTTP 401
    www-authenticate: Bearer realm="OAuth", error="invalid_token"
    {"error": "invalid_token", "error_description": "Missing or invalid access token"}

which is byte-for-byte the shape mcp.monday.com returns.

Run standalone::

    MOCK_MCP_TOKEN=secret-user-token \
        uv run python tests/mocks/bearer_gated_mcp_server.py --port 3399

Then point a ``type=mcp`` connection at ``http://localhost:3399/mcp``
(transport ``streamable_http``).
"""

from __future__ import annotations

import argparse
import json
import os

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

mcp = FastMCP("mock-monday")

# The only token this server accepts. A per-user OAuth connector only ever holds
# it on a UserConnectionCredentials row — never on the connection itself.
VALID_TOKEN = os.environ.get("MOCK_MCP_TOKEN", "secret-user-token")


@mcp.tool(description="List boards visible to the signed-in user.")
def get_boards() -> str:
    return json.dumps([
        {"id": "1001", "name": "Roadmap"},
        {"id": "1002", "name": "Support queue"},
    ])


@mcp.tool(description="Fetch the items on a board.")
def get_board_items(board_id: str) -> str:
    return json.dumps({"board_id": board_id, "items": [{"id": "i1", "name": "Ship it"}]})


@mcp.tool(description="Return the authenticated user.")
def me() -> str:
    return json.dumps({"id": "u1", "name": "Sandbox User"})


def build_app():
    """The MCP ASGI app, gated on a bearer token."""
    from starlette.middleware.base import BaseHTTPMiddleware

    app = mcp.streamable_http_app()

    async def require_bearer(request, call_next):
        auth = request.headers.get("authorization", "")
        token = auth[7:] if auth.lower().startswith("bearer ") else None
        if token != VALID_TOKEN:
            return JSONResponse(
                {
                    "error": "invalid_token",
                    "error_description": "Missing or invalid access token",
                },
                status_code=401,
                headers={
                    "WWW-Authenticate": (
                        'Bearer realm="OAuth", error="invalid_token", '
                        'error_description="Missing or invalid access token"'
                    )
                },
            )
        return await call_next(request)

    app.add_middleware(BaseHTTPMiddleware, dispatch=require_bearer)
    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3399)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
