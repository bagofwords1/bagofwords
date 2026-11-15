"""A real MCP server with an MCP App (``io.modelcontextprotocol/ui``).

Built on the SDK's own ``Apps`` extension, so tools carry ``_meta.ui`` and the
``ui://`` resource is served as ``text/html;profile=mcp-app`` exactly as a
production server would. It speaks MCP 2026-07-28 (stateless) and every older
revision, so it doubles as the "modern server" for protocol tests.

Tools:

* ``show_sales_dashboard`` — renders the Regional sales app (model + app).
* ``refresh_sales`` — app-only (``visibility: ["app"]``), read-only: what the
  app's Refresh button calls. The agent must never see it.
* ``create_followup_task`` — app-only and write-capable: an app call needs the
  user's confirmation in the host.
* ``client_handshake`` — no UI: reports the protocol version and extensions
  the client sent, so a test can see what crossed the wire.

The UI document is ``mcp_apps_sales_app.html`` next to this file.

Run standalone::

    uv run python tests/mocks/mcp_apps_server.py --port 3340
    # → http://127.0.0.1:3340/mcp (transport streamable_http)
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from mcp.server.apps import Apps, ResourceCsp, client_supports_apps
from mcp.server.mcpserver import Context, MCPServer
from mcp.types import CallToolResult, TextContent, ToolAnnotations

APP_URI = "ui://regional-sales/dashboard"
APP_HTML = (Path(__file__).with_name("mcp_apps_sales_app.html")).read_text(encoding="utf-8")

_FOLLOWUPS_FILE = os.environ.get("MOCK_MCP_APPS_FOLLOWUPS_FILE")

_QUARTERS = {
    "Q1": [("North America", 412_300, 1210), ("EMEA", 388_900, 1104), ("APAC", 251_400, 822), ("LATAM", 98_200, 341)],
    "Q2": [("North America", 455_100, 1302), ("EMEA", 402_700, 1150), ("APAC", 298_600, 901), ("LATAM", 121_900, 388)],
    "Q3": [("North America", 471_800, 1344), ("EMEA", 512_400, 1421), ("APAC", 333_000, 975), ("LATAM", 140_250, 402)],
    "Q4": [("North America", 538_900, 1502), ("EMEA", 497_300, 1388), ("APAC", 362_100, 1033), ("LATAM", 166_700, 455)],
}


def sales_rows(quarter: str, region: str = "all") -> list[dict[str, Any]]:
    rows = [
        {"region": r, "revenue": rev, "orders": orders}
        for r, rev, orders in _QUARTERS.get(quarter.upper(), _QUARTERS["Q3"])
    ]
    if region and region.lower() != "all":
        rows = [x for x in rows if x["region"].lower() == region.lower()] or rows
    return rows


def _sales_result(quarter: str, region: str, ui: bool) -> CallToolResult:
    quarter = quarter.upper() if quarter.upper() in _QUARTERS else "Q3"
    rows = sales_rows(quarter, region)
    total = sum(r["revenue"] for r in rows)
    top = max(rows, key=lambda r: r["revenue"])
    summary = (
        f"Regional sales for {quarter}: total revenue ${total:,} across {len(rows)} regions; "
        f"top region {top['region']} (${top['revenue']:,})."
    )
    if ui:
        summary += " An interactive chart is shown to the user."
    return CallToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content={"quarter": quarter, "region": region or "all", "rows": rows},
    )


apps = Apps()


@apps.tool(
    resource_uri=APP_URI,
    description="Show regional sales for one quarter (quarter: Q1, Q2, Q3 or Q4; region: a region name or 'all') as an interactive chart of revenue and orders per region.",
    annotations=ToolAnnotations(readOnlyHint=True),
)
def show_sales_dashboard(ctx: Context, quarter: str = "Q3", region: str = "all") -> CallToolResult:
    return _sales_result(quarter, region, ui=client_supports_apps(ctx))


@apps.tool(
    resource_uri=APP_URI,
    visibility=["app"],
    description="Reload the sales chart for a quarter (called by the app's Refresh button).",
    annotations=ToolAnnotations(readOnlyHint=True),
)
def refresh_sales(ctx: Context, quarter: str = "Q3") -> CallToolResult:
    return _sales_result(quarter, "all", ui=True)


@apps.tool(
    resource_uri=APP_URI,
    visibility=["app"],
    description="Create a follow-up task for a region in the CRM.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False),
)
def create_followup_task(region: str, note: str = "") -> str:
    record = {"region": region, "note": note}
    if _FOLLOWUPS_FILE:
        existing = []
        if os.path.exists(_FOLLOWUPS_FILE):
            try:
                existing = json.loads(Path(_FOLLOWUPS_FILE).read_text())
            except ValueError:
                existing = []
        existing.append(record)
        Path(_FOLLOWUPS_FILE).write_text(json.dumps(existing))
    return f"Created follow-up for {region}: {note or 'no note'}"


apps.add_html_resource(
    APP_URI,
    APP_HTML,
    name="Regional sales",
    description="Interactive regional sales chart",
    csp=ResourceCsp(connect_domains=[], resource_domains=[]),
    prefers_border=True,
)

mcp = MCPServer("regional-sales", extensions=[apps])


@mcp.tool(description="Report the MCP protocol version and extensions this client negotiated.",
          annotations=ToolAnnotations(readOnlyHint=True))
def client_handshake(ctx: Context) -> str:
    caps = ctx.client_capabilities
    extensions = getattr(caps, "extensions", None) if caps else None
    return json.dumps({
        "protocol_version": getattr(ctx, "protocol_version", None),
        "client_supports_apps": client_supports_apps(ctx),
        "extensions": extensions or {},
    })


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3340)
    args = parser.parse_args()
    uvicorn.run(mcp.streamable_http_app(), host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
