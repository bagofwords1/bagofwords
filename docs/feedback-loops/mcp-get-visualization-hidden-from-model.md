# Feedback Loop — BOW-as-MCP: `get_visualization` "missing" for API-token clients

**Report:** "the BagOfWords MCP no longer exposes `get_visualization` (still
missing after a reconnect today). Without it, queries only return a 20-row
preview, which breaks the weekly podcast run. Was it removed or renamed?"

The client authenticates to BOW's own MCP server (`/api/mcp`) with a personal
`bow_` API key, not OAuth.

## Verdict

Not removed, not renamed, and not an auth problem. `get_visualization` is on
the wire for an API-key client, but it is flagged app-only
(`_meta.ui.visibility: ["app"]`, `backend/app/ai/tools/mcp/app_tools.py:82`)
and has been since it was added on 2026-08-23. An MCP host that honors the
MCP Apps visibility flag hides it from the model; one that ignores the flag
lets the model call it. The customer's workflow depended on the second kind
of host. Reconnecting cannot change this because the server sends the same
flag every time.

The "20-row preview" was the hardcoded slice in the MCP `create_data` tool.
It is now `limit` (1–1000, default 1000) with a `truncated` flag
([#1172](https://github.com/bagofwords1/bagofwords/pull/1172)).

## Environment

Backend only, fresh SQLite, pinned encryption key, LLM provider seeded via the
API. The Anthropic key available in this container had no credit
(`400 credit balance is too low`, 6 attempts in the backend log), so the
LLM-backed `create_data` path could not run live. The visualization was
produced without an LLM instead: a builder-mode query run through the real
`POST /api/queries/{id}/run`, plus a directly seeded `Visualization` row
(there is no public route that creates one outside the agent path).

```bash
cd backend
export TESTING=true ENVIRONMENT=production \
  BOW_DATABASE_URL='sqlite:///<scratch>/app.db' TEST_DATABASE_URL="$BOW_DATABASE_URL" \
  BOW_ENCRYPTION_KEY=<fernet key> BOW_CHROMIUM_EXECUTABLE=/opt/pw-browsers/chromium
.venv/bin/python -m alembic upgrade head
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Seed (all through the API): register admin → `POST /api/llm/providers`
(anthropic, one Haiku model) → `POST /api/data_sources/demos/chinook` →
`PUT /api/organization/settings {mcp_enabled: true}` → `POST /api/api_keys`.

MCP client: official TypeScript SDK 1.30 (the library Claude Code / Cursor /
Claude Desktop use), `StreamableHTTPClientTransport`, header
`Authorization: Bearer bow_…`. Scripts: `01_seed.py`, `02_mcp_client.mjs`,
`03_make_viz.py`, `04_get_viz.mjs` (session scratchpad).

## Loop A — what the wire delivers to an API-key client

`tools/list` (TypeScript SDK, `client.listTools()`):

```
count: 13
names: create_report, get_context, inspect_data, create_data, create_artifact,
       edit_artifact, list_agent_tools, execute_mcp, list_instructions,
       create_instruction, delete_instruction, get_visualization, get_artifact_data
get_visualization_present: true
get_visualization_meta: { "ui": { "visibility": ["app"] } }
```

The same list filtered the way an MCP-Apps-aware host filters it for the model
(keep tools whose `_meta.ui.visibility` includes `"model"`):

```
count: 11
hidden: get_visualization, get_artifact_data
```

That is the customer's symptom, reproduced: the tool did not leave the server;
the host stopped showing it to the model.

## Loop B — the server still serves it, and the full data exists

Visualization backed by a 500-row step (`03_make_viz.py 500`):

| Layer | Check | Observed |
|---|---|---|
| HTTP, MCP | `tools/call get_visualization` with the API key | `isError: false`, `data.rows: 500`, columns `row_id, country, amount`, 30,273 chars |
| HTTP, REST | `GET /api/steps/{id}/export` with the same `bow_` key | `200 text/csv`, 501 lines (header + 500) |
| DB | `steps.data` decrypted through the ORM (`import main`, `select(Step)`) | 500 rows, same columns |
| Backend log | `app.routes.mcp` | `tools/call … get_visualization` request logged, 200 |

So `tools/call` does not enforce `_meta.ui.visibility`; only the host does. A
client that bypasses its host filter (or a host that ignores the flag) still
gets every row.

## Loop C — the `limit` contract after #1172

`create_data` could not complete without a funded LLM key, but the input
contract is enforced before any LLM call:

```
create_data {limit: 5000} -> isError: true
  "1 validation error for MCPCreateDataInput / limit
   Input should be less than or equal to 1000"
```

Default and range are pinned by
`backend/tests/unit/test_mcp_create_data_preview_limit.py` (19 cases).
To re-run Loop C live, seed a provider with a funded key and rerun
`02_mcp_client.mjs`: it records `rows_returned`, `total_rows`, `truncated` for
the default limit and for `limit: 5`.

## What this proves / notes

- `get_visualization` is present, app-only, and callable; the disappearance is
  host-side filtering of an app-only tool, so any client update that starts
  honoring MCP Apps visibility produces exactly this report.
- The model-visible surface had no way to read a result beyond the preview.
  #1172 raises the inline preview to 1000 rows with an explicit `limit`.
- Still open: `allow_llm_see_data` is not applied to the MCP preview, and
  results above 1000 rows have no model-visible path other than the REST
  export.
- Do not flip `get_visualization` to model-visible as a fix: it returns code,
  chart config, and unbounded rows with no paging.
