"""A real streamable-HTTP MCP server with 10 deliberately hard tool schemas.

This is the **oracle** for the MCP argument-fidelity feedback loop. Unlike
``echo_mcp_http_server`` (one trivial tool, proves headers travel), every tool
here reproduces a schema shape that real MCP servers use and that agents get
wrong: JSON-encoded string fields, integer enums, ``$ref``/``$defs`` nesting,
unix-timestamp integers, arrays of objects, pattern-constrained strings.

Each call is validated against the tool's declared JSON Schema *server-side* and
recorded to ``MOCK_MCP_CAPTURE_FILE`` as one JSONL line:

    {"tool", "arguments", "valid", "violations": [{"path", "message"}], "ts"}

That file is the score sheet: "first-call argument validity" is just the
fraction of first-attempt calls per tool with ``valid == true``. Invalid calls
come back as ``isError`` with a vendor-flavored message (terse on purpose — real
servers do not hand you a corrected schema).

Run standalone::

    MOCK_MCP_CAPTURE_FILE=/tmp/bow-agent/mcp_calls.jsonl \
        uv run python tests/mocks/complex_schema_mcp_server.py --port 3334

Then point a ``type=mcp`` connection at ``http://localhost:3334/mcp``
(transport ``streamable_http``).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from datetime import datetime, timezone
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

_CAPTURE_FILE = os.environ.get("MOCK_MCP_CAPTURE_FILE")


# ---------------------------------------------------------------------------
# Tool schemas
#
# Every schema below is hand-written (not derived from type hints) so the exact
# wire shape is under test — including the `$ref`/`$defs` indirection that
# zod-to-json-schema servers emit and that a text-rendered schema loses.
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        # FAILURE MODE: JSON-encoded string. The classic monday.com case — the
        # field is a *string* whose content is JSON, not an object.
        "name": "board_change_column_values",
        "description": "Change multiple column values of a board item. Returns the updated item.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "boardId": {"type": "string", "description": "The board's unique identifier."},
                "itemId": {"type": "string", "description": "The item's unique identifier."},
                "columnValues": {
                    "type": "string",
                    "description": "The column values to update, keyed by column id.",
                },
                "createLabelsIfMissing": {
                    "type": "boolean",
                    "description": "Create status/dropdown labels that do not exist yet.",
                    "default": False,
                },
            },
            "required": ["boardId", "itemId", "columnValues"],
            "additionalProperties": False,
        },
    },
    {
        # FAILURE MODE: integer enum (not a string label) + $ref to a nested def.
        "name": "issue_create",
        "description": "Create a new issue in a team's backlog.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "teamId": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "priority": {
                    "type": "integer",
                    "enum": [0, 1, 2, 3, 4],
                    "description": "0=None, 1=Urgent, 2=High, 3=Normal, 4=Low.",
                },
                "labelIds": {"type": "array", "items": {"type": "string"}},
                "estimate": {"$ref": "#/$defs/Estimate"},
            },
            "required": ["teamId", "title", "priority"],
            "additionalProperties": False,
            "$defs": {
                "Estimate": {
                    "type": "object",
                    "properties": {
                        "value": {"type": "number"},
                        "unit": {"type": "string", "enum": ["points", "hours", "days"]},
                    },
                    "required": ["value", "unit"],
                    "additionalProperties": False,
                }
            },
        },
    },
    {
        # FAILURE MODE: nested object whose inner field is a constrained enum,
        # plus a bounded integer.
        "name": "search_workspace",
        "description": "Search pages and databases across the workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "filter": {
                    "type": "object",
                    "properties": {
                        "property": {"type": "string", "enum": ["object"]},
                        "value": {"type": "string", "enum": ["page", "database"]},
                    },
                    "required": ["property", "value"],
                    "additionalProperties": False,
                },
                "pageSize": {"type": "integer", "minimum": 1, "maximum": 100, "default": 25},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        # FAILURE MODE: pattern-constrained string that LOOKS like a free-form
        # duration, plus an enum that overlaps with common natural phrasings.
        "name": "find_errors",
        "description": "Find errors in a project over a rolling stats window.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "organizationSlug": {"type": "string"},
                "projectSlug": {"type": "string"},
                "statsPeriod": {
                    "type": "string",
                    "pattern": "^[0-9]+[hd]$",
                    "description": "Rolling window, digits followed by h or d. e.g. '24h', '14d'.",
                },
                "sortBy": {"type": "string", "enum": ["last_seen", "first_seen", "count", "userCount"]},
            },
            "required": ["organizationSlug", "projectSlug", "statsPeriod"],
            "additionalProperties": False,
        },
    },
    {
        # FAILURE MODE: second JSON-encoded string field, different vendor
        # flavour (Atlassian `fields`).
        "name": "jira_edit_issue",
        "description": "Edit fields on an existing Jira issue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "issueIdOrKey": {"type": "string"},
                "fields": {
                    "type": "string",
                    "description": "The issue fields to set.",
                },
                "notifyUsers": {"type": "boolean", "default": True},
            },
            "required": ["issueIdOrKey", "fields"],
            "additionalProperties": False,
        },
    },
    {
        # FAILURE MODE: unix-epoch INTEGERS where a model reaches for ISO dates.
        "name": "metrics_query",
        "description": "Query a timeseries of metrics over a time range.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Metric query, e.g. 'avg:system.cpu.user{*}'."},
                "from": {"type": "integer", "description": "Start of the range."},
                "to": {"type": "integer", "description": "End of the range."},
            },
            "required": ["query", "from", "to"],
            "additionalProperties": False,
        },
    },
    {
        # FAILURE MODE: two levels of $ref/$defs indirection.
        "name": "contact_upsert",
        "description": "Create or update a CRM contact record.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "email": {"type": "string", "format": "email"},
                "displayName": {"type": "string"},
                "address": {"$ref": "#/$defs/Address"},
            },
            "required": ["email", "displayName"],
            "additionalProperties": False,
            "$defs": {
                "Address": {
                    "type": "object",
                    "properties": {
                        "line1": {"type": "string"},
                        "city": {"type": "string"},
                        "countryCode": {"type": "string", "pattern": "^[A-Z]{2}$"},
                        "geo": {"$ref": "#/$defs/Geo"},
                    },
                    "required": ["line1", "city", "countryCode"],
                    "additionalProperties": False,
                },
                "Geo": {
                    "type": "object",
                    "properties": {"lat": {"type": "number"}, "lng": {"type": "number"}},
                    "required": ["lat", "lng"],
                    "additionalProperties": False,
                },
            },
        },
    },
    {
        # FAILURE MODE: array of objects, each with its own required keys —
        # models commonly flatten this to an array of strings.
        "name": "analytics_run_report",
        "description": "Run an analytics report over dimensions and metrics.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "propertyId": {"type": "string"},
                "dimensions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
                "metrics": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"name": {"type": "string"}},
                        "required": ["name"],
                        "additionalProperties": False,
                    },
                },
                "dateRanges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "startDate": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                            "endDate": {"type": "string", "pattern": "^[0-9]{4}-[0-9]{2}-[0-9]{2}$"},
                        },
                        "required": ["startDate", "endDate"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["propertyId", "metrics"],
            "additionalProperties": False,
        },
    },
    {
        # FAILURE MODE: array items behind a $ref, with a discriminated shape.
        "name": "records_batch_update",
        "description": "Apply a batch of record operations atomically.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "tableId": {"type": "string"},
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"$ref": "#/$defs/Operation"},
                },
            },
            "required": ["tableId", "operations"],
            "additionalProperties": False,
            "$defs": {
                "Operation": {
                    "type": "object",
                    "properties": {
                        "op": {"type": "string", "enum": ["set", "delete"]},
                        "recordId": {"type": "string"},
                        "field": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["op", "recordId"],
                    "additionalProperties": False,
                }
            },
        },
    },
    {
        # FAILURE MODE: JSON-encoded string payload + pattern-constrained key.
        "name": "workflow_trigger",
        "description": "Trigger a workflow run with a payload.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "workflowId": {"type": "string"},
                "payload": {
                    "type": "string",
                    "description": "The run payload.",
                },
                "idempotencyKey": {
                    "type": "string",
                    "pattern": "^[a-z0-9]{8,32}$",
                    "description": "Lowercase alphanumeric, 8-32 chars.",
                },
            },
            "required": ["workflowId", "payload", "idempotencyKey"],
            "additionalProperties": False,
        },
    },
    {
        # FAILURE MODE: the schema LIES BY OMISSION. Every property is optional
        # — there is no `required` key at all — but the server rejects a call
        # that supplies only pagination. Copied from the real Intercom
        # search_contacts tool, which produced exactly this failure in
        # production: `{"per_page": 50}` is schema-valid and still 400s.
        #
        # This is the case neither inline schemas nor native registration can
        # catch, because there is nothing in the schema to validate against. It
        # exists to exercise the mcp_failed_then_fixed instruction trigger.
        "name": "contacts_search",
        "description": "Search for contacts using multiple filter criteria.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "contact_ids": {"type": "array", "items": {"type": "string"}},
                "name": {"type": "string", "description": "Search contacts by name (partial match)."},
                "email": {"type": "string", "description": "Search contacts by exact email address."},
                "email_domain": {"type": "string"},
                "phone": {"type": "string"},
                "per_page": {"type": "number", "default": 5, "description": "Results per page (max 150)."},
            },
            "additionalProperties": False,
        },
    },
    {
        # The recovery path — deliberately NOT an obvious "list_all". Intercom
        # has no list-contacts tool; the only way to enumerate is a search DSL
        # whose entry point is easy to overlook. An obviously-named listing tool
        # here would let the agent sidestep the failure entirely and the trigger
        # would never be exercised (it did exactly that on the first attempt).
        "name": "workspace_query",
        "description": (
            "Run a workspace query using the Query DSL. The query MUST start with "
            "object_type:contacts or object_type:conversations to select which API to "
            "call, followed by optional space-separated key[:op]:value filters. "
            "Example: 'object_type:contacts' returns all contacts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Query DSL string."},
                "per_page": {"type": "number", "default": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]

# Tools whose runtime contract is stricter than their declared schema. The value
# is (predicate over arguments, error message) — when the predicate holds, the
# call is rejected even though it validates. Real servers do this constantly;
# modelling it is the only way to test the failure class honestly.
UNDECLARED_CONSTRAINTS = {
    "contacts_search": (
        lambda a: not any(
            k in a for k in ("contact_ids", "name", "email", "email_domain", "phone")
        ),
        "At least one search parameter must be provided",
    ),
}

# Optional filler so the catalog can be pushed past the <mcp_tools> inline-schema
# threshold (40). Above it, the gateway path stops inlining schemas and the agent
# must rediscover them via search_mcps — which is the regime real customers with
# monday/Atlassian-sized catalogs are actually in, and the one where native
# registration is supposed to earn its keep. Set MOCK_MCP_PAD_TOOLS=N.
def _padding_tools(n: int) -> list[dict[str, Any]]:
    domains = ["orders", "invoices", "tickets", "assets", "vendors",
               "shipments", "budgets", "policies", "incidents", "reviews"]
    verbs = ["list", "get", "search", "update", "archive"]
    out: list[dict[str, Any]] = []
    for i in range(n):
        domain = domains[i % len(domains)]
        verb = verbs[(i // len(domains)) % len(verbs)]
        out.append({
            "name": f"{domain}_{verb}_{i}",
            "description": f"{verb.capitalize()} {domain} records in the workspace.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "recordId": {"type": "string", "description": f"The {domain} record id."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    "includeArchived": {"type": "boolean", "default": False},
                },
                "required": ["recordId"],
                "additionalProperties": False,
            },
        })
    return out


_PAD = int(os.environ.get("MOCK_MCP_PAD_TOOLS", "0") or 0)
if _PAD > 0:
    TOOLS.extend(_padding_tools(_PAD))

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# ---------------------------------------------------------------------------
# Validation + capture
# ---------------------------------------------------------------------------

def _violations(tool_name: str, arguments: dict[str, Any]) -> list[dict[str, str]]:
    """Validate arguments against the tool's declared schema."""
    from jsonschema import Draft202012Validator

    schema = TOOLS_BY_NAME[tool_name]["inputSchema"]
    validator = Draft202012Validator(schema)
    out: list[dict[str, str]] = []
    for err in sorted(validator.iter_errors(arguments), key=lambda e: list(e.absolute_path)):
        path = ".".join(str(p) for p in err.absolute_path) or "<root>"
        out.append({"path": path, "message": err.message})
    return out


def _capture(record: dict[str, Any]) -> None:
    if not _CAPTURE_FILE:
        return
    try:
        directory = os.path.dirname(_CAPTURE_FILE)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(_CAPTURE_FILE, "a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except Exception:
        pass


def _success_payload(tool_name: str, arguments: dict[str, Any]) -> Any:
    """A plausible success result. Content is irrelevant to the metric —
    what matters is that a valid call is clearly distinguishable from an
    invalid one in the transcript."""
    return {
        "ok": True,
        "tool": tool_name,
        "echo": arguments,
        "note": "arguments validated against the declared input schema",
    }


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

server = Server("complex-schema-mock")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["inputSchema"],
        )
        for t in TOOLS
    ]


# validate_input=False is essential: the SDK's default handler validates against
# inputSchema and short-circuits BEFORE our function runs, so invalid calls would
# never be captured — and invalid calls are the entire measurement. We take over
# validation so every attempt, valid or not, lands in the capture file.
@server.call_tool(validate_input=False)
async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.ContentBlock]:
    arguments = arguments or {}

    if name not in TOOLS_BY_NAME:
        _capture({
            "tool": name, "arguments": arguments, "valid": False,
            "violations": [{"path": "<root>", "message": "unknown tool"}],
            "ts": datetime.now(timezone.utc).isoformat(),
        })
        raise ValueError(f"Unknown tool: {name}")

    violations = _violations(name, arguments)

    # A schema-valid call can still be refused by the server's real contract.
    # Recorded separately so scoring can distinguish "the agent broke the
    # schema" from "the schema never described the rule".
    undeclared = None
    if not violations and name in UNDECLARED_CONSTRAINTS:
        predicate, message = UNDECLARED_CONSTRAINTS[name]
        if predicate(arguments):
            undeclared = message

    _capture({
        "tool": name,
        "arguments": arguments,
        "valid": not violations,
        "violations": violations,
        "undeclared_constraint": undeclared,
        "ts": datetime.now(timezone.utc).isoformat(),
    })

    if undeclared:
        raise ValueError(undeclared)

    if violations:
        # Deliberately terse and vendor-flavoured: a real server tells you
        # something is wrong, not how to fix it. This is what makes the
        # recovery loop expensive.
        first = violations[0]
        raise ValueError(f"400 Bad Request: invalid value for '{first['path']}'")

    return [types.TextContent(type="text", text=json.dumps(_success_payload(name, arguments)))]


def build_app():
    from starlette.applications import Starlette
    from starlette.routing import Mount

    manager = StreamableHTTPSessionManager(app=server, json_response=False, stateless=True)

    async def handle(scope, receive, send):
        await manager.handle_request(scope, receive, send)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=handle)], lifespan=lifespan)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=3334)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    import uvicorn

    uvicorn.run(build_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
