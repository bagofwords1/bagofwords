#!/usr/bin/env python3
"""A real MCP server (streamable HTTP) with one synthetic billing tool.

Used by mcp_beside_sql_loop.py to stand in for an external billing MCP the
customer attaches as an agent. Every call is appended to MCP_COST_LOG (one
JSON line per call), so the loop can prove the tool really ran.

    cd backend && MCP_COST_PORT=9310 uv run python ../tools/agent/mcp_cost_server.py

Endpoint: http://127.0.0.1:$MCP_COST_PORT/mcp
"""
from __future__ import annotations

import json
import os
import time

from mcp.server.mcpserver import MCPServer

LOG = os.environ.get("MCP_COST_LOG", "/tmp/bow-agent/mcp_cost_calls.jsonl")
PORT = int(os.environ.get("MCP_COST_PORT", "9310"))

server = MCPServer("cost-server")


@server.tool()
def cost_report(days: int = 30) -> dict:
    """Total cloud spend per service over the last `days` days (synthetic)."""
    with open(LOG, "a") as f:
        f.write(json.dumps({"tool": "cost_report", "days": days, "at": time.time()}) + "\n")
    return {"days": days, "rows": [{"service": "compute", "cost": 1234.5}, {"service": "storage", "cost": 321.0}]}


if __name__ == "__main__":
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    server.run(transport="streamable-http", host="127.0.0.1", port=PORT)
