"""A real streamable-HTTP MCP server whose tools return every content-block shape.

Each tool answers with one kind of MCP tool-result content, the way real
servers do, so a test can check what reaches the agent for each:

* ``get_file_contents`` — GitHub's shape: a short text status line plus the
  file body as an embedded *text* resource.
* ``take_screenshot`` — a single image block (base64 PNG).
* ``export_pdf`` — text plus an embedded *blob* resource (a PDF).
* ``link_dataset`` — a resource link instead of the content itself.
* ``three_charts`` — three image blocks in one result.
* ``export_zip`` — an embedded blob of a type the app cannot open.

Run standalone::

    uv run python tests/mocks/content_mcp_http_server.py --port 3334

Then point a ``type=mcp`` connection at ``http://localhost:3334/mcp``.
"""

from __future__ import annotations

import argparse
import base64

from mcp.server.fastmcp import FastMCP
from mcp.types import (
    BlobResourceContents,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
    TextResourceContents,
)

mcp = FastMCP("mock-content")

README_TEXT = (
    "# Bag of Words\n\n"
    "Chat with your data - with memory, rules, and observability built in.\n\n"
    "MARKER-README-BODY-7f3a\n"
)

# A real 1x1 PNG so image renderers can decode it.
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode()

PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


@mcp.tool()
def get_file_contents(owner: str, repo: str, path: str):
    """Read a file from a repository (GitHub MCP shape)."""
    return [
        TextContent(type="text", text="successfully downloaded text file (SHA: 6fa68e19b083d49c)"),
        EmbeddedResource(
            type="resource",
            resource=TextResourceContents(
                uri=f"repo://{owner}/{repo}/contents/{path}",
                mimeType="text/markdown",
                text=README_TEXT,
            ),
        ),
    ]


@mcp.tool()
def take_screenshot(url: str):
    """Screenshot a page."""
    return [ImageContent(type="image", data=PNG_B64, mimeType="image/png")]


@mcp.tool()
def export_pdf(report: str):
    """Export a report as PDF."""
    return [
        TextContent(type="text", text=f"exported {report}"),
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=f"reports://{report}.pdf",
                mimeType="application/pdf",
                blob=base64.b64encode(PDF_BYTES).decode(),
            ),
        ),
    ]


@mcp.tool()
def link_dataset(name: str):
    """Point at a dataset without embedding it."""
    return [
        ResourceLink(
            type="resource_link",
            uri=f"data://datasets/{name}.csv",
            name=f"{name}.csv",
            mimeType="text/csv",
        )
    ]


@mcp.tool()
def three_charts(topic: str):
    """Render three charts."""
    return [ImageContent(type="image", data=PNG_B64, mimeType="image/png") for _ in range(3)]


@mcp.tool()
def export_zip(name: str):
    """Export an archive the app cannot open."""
    return [
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=f"exports://{name}.zip",
                mimeType="application/zip",
                blob=base64.b64encode(b"PK\x03\x04fake").decode(),
            ),
        )
    ]


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3334)
    args = parser.parse_args()
    mcp.settings.host = "127.0.0.1"
    mcp.settings.port = args.port
    mcp.run(transport="streamable-http")
