# Sandbox Feedback Loop — Reports thread: ExecuteMCP shows generic icon + raw tool names

Reproduces the reported UI issue on `/reports/[id]`: when the agent calls
`execute_mcp`, the tool row in the chat thread renders:

1. **Generic icon.** A gray `heroicons-server-stack` glyph
   (`frontend/components/tools/MCPTool.vue:10,14`) — even when the connection is
   a known catalog connector (Monday, Notion, …) whose brand icon the app
   already ships (`frontend/components/DataSourceIcon.vue`,
   `CONNECTOR_ICON_FILE` map).
2. **Unreadable title.** The row label is `result_json.connection_name ||
   arguments_json.tool_name` (`MCPTool.vue:106-133`), and on **every successful
   call the backend sets `connection_name`** (`execute_mcp.py:206`) — so each
   row is *just the connection name*. A turn that calls three Monday tools
   renders three identical, indistinguishable rows:

   ```
   Monday  2.3s  >
   Monday  4.8s  >
   Monday  1.6s  >
   ```

   Which tool ran (`prompt_builder_v3(...)`, `agent_v2(...)`) is only visible
   after expanding a row and its Input section. The **raw MCP tool name**
   surfaces as the title only on the fallback path — failures ("agent_v2
   (failed)") and mid-run streaming before `connection_resolved` arrives
   ("Calling prompt_builder_v3…") — which is where the cryptic
   `prompt_builder_v3` / `agent_v2` labels come from.

This doc is the runnable loop used to reproduce both symptoms in a fresh cloud
sandbox, with Playwright screenshots. **No fix is implemented here.**

---

## Where it lives

| Piece | File |
|---|---|
| Tool row component (icon + label) | `frontend/components/tools/MCPTool.vue` |
| Component wiring — note it already receives `:data-sources="report?.data_sources"` | `frontend/pages/reports/[id]/index.vue:296-313` |
| Brand-icon renderer keyed by `connector_key` | `frontend/components/DataSourceIcon.vue` |
| `connection_name` streamed mid-run (`connection_resolved` stage) | `backend/app/ai/tools/implementations/execute_mcp.py:143`, captured at `index.vue:2251` |
| `connector_key` derivation (`config.catalog_key` / preset server URL) | `backend/app/services/data_source_service.py:_conn_connector_key` |

### Backend gap found while reproducing

`ReportService.get_report` (`backend/app/services/report_service.py:348`)
passes raw ORM objects into `ReportSchema(data_sources=report.data_sources)`.
`DataSourceReportSchema.connector_key` / `ConnectionEmbedded.connector_key`
are **computed only in `data_source_service`** (never stored on the model), so
in the **report payload they are always `null`** — confirmed live:

```json
{"name": "Monday", "type": "mcp", "connector_key": null,
 "config": {"server_url": "https://mcp.monday.com/mcp", "catalog_key": "monday"}}
```

However `ConnectionEmbedded.config` **is** serialized in the report response
and contains `catalog_key` — so a frontend-only icon fix is possible today by
matching `result_json.connection_name` → `report.data_sources[*].connections[*]`
and reading `config.catalog_key` (or, cleaner, populating `connector_key` in
the report serialization path too).

---

## Environment setup (fresh sandbox)

Per `docs/design/sandbox-feedback-loop.md` — backend on Python 3.12, local
auth, SQLite; no LLM key needed (blocks are seeded directly).

```bash
# Backend
cd backend
python3.12 -m venv .venv && .venv/bin/pip install uv && .venv/bin/uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD="dummy"
mkdir -p db uploads/files uploads/branding
.venv/bin/alembic upgrade head
.venv/bin/python main.py &

# configs/bow-config.dev.yaml (sandbox-only, uncommitted):
#   features.allow_uninvited_signups: true   (auth.mode is already "local_only")

# Frontend — npm, not yarn: yarn classic pulls every platform's native
# binaries and stalls on the proxied network (same hurdle as
# sandbox-feedback-loop-reports-branding.md)
cd ../frontend && npm install --legacy-peer-deps && npx nuxt dev &
npx playwright install chromium
```

Register `sandbox@bow.dev` / login / grab org id via the standard curl calls in
`docs/design/sandbox-feedback-loop.md`.

## Seed

`scratchpad/seed_mcp_repro.py` (run with the backend venv from `backend/`)
inserts directly into SQLite via the ORM:

- **Connection** `Monday` (`type="mcp"`,
  `config={"server_url": "https://mcp.monday.com/mcp", "catalog_key": "monday"}`)
  + three `ConnectionTool` rows (`prompt_builder_v3`, `agent_v2`,
  `get_board_items_by_name`).
- **DataSource** `Monday` linked via `domain_connection`, and a **Report**
  linked via `report_data_source_association`.
- A user completion + system completion with an `AgentExecution` and **three
  `execute_mcp` ToolExecutions** (`prompt_builder_v3`, `agent_v2`,
  `get_board_items_by_name`) wired through `CompletionBlock`s — each with the
  exact `result_json` shape the backend persists on success
  (`execute_mcp.py:203-212`), i.e. `connection_name: "Monday"` set on all
  three.

Gotchas hit while seeding (so you don't):

- There is no `app/models/__init__.py`; import every model module explicitly
  (pkgutil walk) or mapper config fails on dangling relationship strings.
- **Skip `app/models/application.py`** — it references a
  `DataSourceApplicationAssociation` class that doesn't exist anywhere; the app
  itself never imports it.
- `ToolExecution.agent_execution_id` is NOT NULL — a completion block's tool
  execution must hang off a real `AgentExecution`.

## Loop — visual validation (Playwright + Claude's eyes)

`scratchpad/shot.mjs`: sign in at `/users/sign-in`, click **Skip onboarding**
(fresh org redirects to `/onboarding` because no LLM is configured), open
`/reports/<id>` with `waitUntil: 'domcontentloaded'` + wait for
`.tool-execution-container` (dev-server first compile breaks `networkidle`),
screenshot collapsed, click both tool headers, screenshot expanded.

```bash
RID=<report_id> node scratchpad/shot.mjs
```

### Observed (live app) — bug confirmed

Collapsed thread (`mcp-collapsed.png`) — one turn, three Monday tool calls:

```
[server-stack icon] Monday  2.3s  >
[server-stack icon] Monday  4.8s  >
[server-stack icon] Monday  1.6s  >
```

- All rows render the generic gray `heroicons-server-stack` icon even though
  the connection's `config.catalog_key` is `monday` and
  `/data_sources_icons/monday.svg` ships with the app (DataSourceIcon renders
  it everywhere else — connection modals, Knowledge Explorer).
- All three titles are identical — just the connection name. There is no way
  to tell which MCP tool each row invoked, or that they differ at all.

Expanded (`mcp-expanded.png`): only after clicking a row **and** its Input
section do you see `prompt_builder_v3({"query": …})` — the tool identity is
two clicks deep, rendered as a raw code-style call. The raw-name-as-title
variant (`agent_v2` as the header) reproduces by omitting
`result_json.connection_name`, which in production happens on failed calls
and mid-stream before the `connection_resolved` event.

---

## What this proves

- The success path — the shape `execute_mcp` always persists — renders N
  identical "<connection name>" rows per turn; the reported
  `prompt_builder_v3`/`agent_v2` labels are the same component's fallback
  when `connection_name` is absent (failures, mid-stream). Both reproduce
  with plain persisted data — no streaming or LLM needed.
- The brand icon asset, the `connector_key` derivation, and the
  `data-sources` prop plumbing into the tool component **all already exist**;
  MCPTool.vue just never uses them.
- One real backend gap for a clean fix: report responses don't populate
  `connector_key` (though `config.catalog_key` is present in the payload).

## Fix directions (not implemented)

1. **Icon (frontend-only).** MCPTool accepts the already-passed `dataSources`
   prop, resolves the connection by `result_json.connection_name` /
   `arguments_json.connection_id` among `type in (mcp, custom_api)`
   connections, and renders `<DataSourceIcon type="mcp" :connector-key>` with
   the current glyph as fallback. Optionally have the backend include
   `connector_key` in the `connection_resolved` progress payload (precedent:
   `read_resources` streams an `icon` field) and in report serialization.
2. **Title.** Deterministic humanization as the baseline — strip `_v\d+`,
   underscores → spaces, title-case, and combine with the connection:
   **"Monday — Prompt Builder"**. Works retroactively for all persisted rows.
   Optionally add an LLM-supplied `display_title` to `ExecuteMCPInput`
   (like `create_artifact.title`) preferred when present.
