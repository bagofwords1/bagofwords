#!/usr/bin/env python3
"""A tiny Jira-like MCP server (streamable HTTP) for per-agent tool-rule checks.

Tools: ``search_issues`` (read-only), ``create_issue`` and ``add_comment``
(writes). Every call is appended to ``MOCK_JIRA_LOG`` (JSON lines) so a check
can prove exactly which calls reached the server.

    cd backend && MOCK_JIRA_LOG=/tmp/jira_calls.jsonl \
        uv run python ../tools/agent/mock_jira_mcp.py --port 8766
    # connection: type=mcp, server_url=http://localhost:8766/mcp
"""
import argparse
import itertools
import json
import os
import time

from mcp.server.mcpserver import MCPServer as FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("mock-jira")
_LOG = os.environ.get("MOCK_JIRA_LOG")
_ids = itertools.count(101)
_ISSUES = [
    {"key": "OPS-41", "summary": "Nightly ETL failed on orders table", "status": "Open"},
    {"key": "OPS-42", "summary": "Disk 90% on reporting replica", "status": "Open"},
    {"key": "FIN-7", "summary": "Q3 accrual mismatch", "status": "In review"},
]


def _log(tool, args):
    if _LOG:
        with open(_LOG, "a") as fh:
            fh.write(json.dumps({"t": time.time(), "tool": tool, "arguments": args}) + "\n")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True), description="Search Jira issues by text.")
def search_issues(query: str = "") -> str:
    _log("search_issues", {"query": query})
    q = query.lower()
    return json.dumps([i for i in _ISSUES if not q or q in json.dumps(i).lower()])


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), description="Create a Jira issue in a project.")
def create_issue(project: str, summary: str, description: str = "") -> str:
    _log("create_issue", {"project": project, "summary": summary, "description": description})
    key = f"{project.upper()}-{next(_ids)}"
    _ISSUES.append({"key": key, "summary": summary, "status": "Open"})
    return json.dumps({"created": key, "summary": summary})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False), description="Add a comment to a Jira issue.")
def add_comment(issue_key: str, body: str) -> str:
    _log("add_comment", {"issue_key": issue_key, "body": body})
    return json.dumps({"commented": issue_key})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8766)
    a = ap.parse_args()
    mcp.run(transport="streamable-http", host="127.0.0.1", port=a.port)
