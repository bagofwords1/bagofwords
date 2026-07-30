"""A real streamable-HTTP MCP server that ECHOES what it received over the wire.

Unlike ``mock_mcp_server.MockToolProviderClient`` (an in-process stub), this is a
genuine MCP server reachable over HTTP, so it proves that forwarded **headers**
and injected **custom_metadata** actually travel the wire. It is the oracle for
the user-context-forwarding feedback loop and the Playwright E2E.

It exposes one tool, ``query_production_orders``, whose result contains exactly
the ``arguments`` (including ``custom_metadata``) and the request ``headers`` the
server saw. The same capture is written to ``MOCK_MCP_CAPTURE_FILE`` so a test can
assert on it out-of-band (the app materializes tool output into CSV/preview).

Run standalone::

    MOCK_MCP_CAPTURE_FILE=/tmp/bow-agent/mcp_capture.json \
        uv run python tests/mocks/echo_mcp_http_server.py --port 3333

Then point a ``type=mcp`` connection at ``http://localhost:3333/mcp``
(transport ``streamable_http``).
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Optional

from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("mock-ln")

_CAPTURE_FILE = os.environ.get("MOCK_MCP_CAPTURE_FILE")


def _request_headers(ctx: Optional[Context]) -> dict[str, str]:
    """Best-effort read of the incoming HTTP headers (lowercased keys)."""
    if ctx is None:
        return {}
    try:
        request = ctx.request_context.request  # Starlette Request on HTTP transports
        if request is not None:
            return {k.lower(): v for k, v in request.headers.items()}
    except Exception:
        pass
    return {}


def _write_capture(payload: dict[str, Any]) -> None:
    if not _CAPTURE_FILE:
        return
    try:
        directory = os.path.dirname(_CAPTURE_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(_CAPTURE_FILE, "w") as fh:
            json.dump(payload, fh, indent=2, default=str)
    except Exception:
        pass


@mcp.tool(
    description="Query LN production orders. Echoes the arguments and headers it received.",
)
def query_production_orders(
    prompt: str,
    company: str = "",
    custom_metadata: Optional[dict] = None,
    ctx: Context = None,
) -> str:
    captured = {
        "received_arguments": {
            "prompt": prompt,
            "company": company,
            "custom_metadata": custom_metadata or {},
        },
        "received_headers": _request_headers(ctx),
    }
    _write_capture(captured)
    return json.dumps(captured)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3333)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.run(transport="streamable-http")  # served at http://host:port/mcp


if __name__ == "__main__":
    main()
