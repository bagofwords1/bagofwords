"""
Mock "regular" MCP connector server (Monday-like) for sandbox e2e verification.

Speaks the real MCP protocol via FastMCP over streamable-http, so the app's
real McpClient (mcp SDK) talks to it unchanged. Supports an OPTIONAL bearer
token (tier A/B "admin app" / "api key" style) via MOCK_BEARER.

Run:
    MOCK_PORT=9301 MOCK_BEARER=secret-monday-token \
      uv run python tools/sandbox/connector-mocks/mock_mcp_regular.py

Connect a connection with:
    type="mcp", transport="streamable_http",
    server_url="http://localhost:9301/mcp",
    credentials={"token": "secret-monday-token"}   # if MOCK_BEARER set
"""
import os
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Mock Monday", stateless_http=True)

# ---- In-memory "Monday" data ----
_BOARDS = [
    {"id": "b1", "name": "Engineering", "items": 3},
    {"id": "b2", "name": "Marketing", "items": 2},
]
_ITEMS = {
    "b1": [
        {"id": "i1", "title": "Ship connectors", "status": "Working on it", "owner": "Dana"},
        {"id": "i2", "title": "Fix OAuth bug", "status": "Done", "owner": "Lee"},
        {"id": "i3", "title": "Write tests", "status": "Stuck", "owner": "Sam"},
    ],
    "b2": [
        {"id": "i4", "title": "Launch blog", "status": "Working on it", "owner": "Max"},
        {"id": "i5", "title": "SEO audit", "status": "Done", "owner": "Max"},
    ],
}


@mcp.tool()
def list_boards() -> list:
    """List all Monday boards with their item counts."""
    return _BOARDS


@mcp.tool()
def get_items(board_id: str) -> list:
    """Get all items (rows) on a Monday board by board_id (e.g. 'b1')."""
    return _ITEMS.get(board_id, [])


@mcp.tool()
def create_item(board_id: str, title: str) -> dict:
    """Create a new item on a Monday board. This is a WRITE action."""
    new = {"id": f"new-{len(_ITEMS.get(board_id, []))+1}", "title": title,
           "status": "Working on it", "owner": "you"}
    _ITEMS.setdefault(board_id, []).append(new)
    return {"created": True, "item": new}


def _build_app():
    app = mcp.streamable_http_app()
    bearer = os.environ.get("MOCK_BEARER")
    if bearer:
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import JSONResponse

        class BearerAuth(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                # Allow unauthenticated discovery/health; gate the MCP endpoint.
                auth = request.headers.get("authorization", "")
                if not auth.startswith("Bearer ") or auth.split(" ", 1)[1] != bearer:
                    return JSONResponse({"error": "invalid_token"}, status_code=401)
                return await call_next(request)

        app.add_middleware(BearerAuth)
    return app


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MOCK_PORT", "9301"))
    uvicorn.run(_build_app(), host="0.0.0.0", port=port, log_level="warning")
