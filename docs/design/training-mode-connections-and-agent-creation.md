# Training mode — list connections, create agent, set tables/tools

**Status:** Design — not implemented.
**Branch:** `claude/training-mode-connections-yodm9m`

Extends **training mode** so the AI can *build* an agent, not just curate one:

1. **`list_connections`** — show the org connections the training actor can build on
   ("resolve to"), with enough catalog detail to choose tables/tools.
2. **`create_agent`** — create a new agent (fka data source) linked to one or more
   existing connections, and attach it to the current training session.
3. **`set_agent_tables` / `set_agent_tools`** — select the agent's active tables
   (relational connections) or enabled tools + policy (MCP / custom_api connections),
   based on the connection's catalog.

This follows the exact pattern of the prompts training tools
(`docs/feedback-loops/prompts-training-tools.md`) and the training-scoping rules of
`docs/feedback-loops/training-mode-agent-admins.md`.

---

## Grounding (what exists today)

| Concept | Entity | Key facts |
| --- | --- | --- |
| Agent | `DataSource` (`backend/app/models/data_source.py:14`) | Container: name, publish/visibility, instructions, memberships. **No** type/config/credentials — those live on `Connection`. Unique `(org, name)`. |
| Connection | `Connection` (`backend/app/models/connection.py:9`) | `type`, `config`, encrypted `credentials`, `auth_policy`. Org-owned. M:N to agents via `domain_connection`. |
| Table catalog | `ConnectionTable` → `DataSourceTable` | Connection-level discovered schema; per-agent selection via `DataSourceTable.is_active`. |
| Tool catalog | `ConnectionTool` → `DataSourceConnectionTool` | Connection-level discovered tools (`is_enabled`, `policy ∈ allow\|confirm\|deny`); per-agent overlay. |

Services to reuse unchanged:

- `ConnectionService.get_connections` (`connection_service.py:334`) — org list; the
  HTTP route (`routes/connection.py:115`) filters visibility in-body (admins see all;
  members see granted / DS-backed connections).
- `DataSourceService.create_data_source` (`data_source_service.py:453`) — **Mode 2**
  (link existing `connection_ids`) already: enforces the license agent cap, seeds
  `DataSourceTable` from the connection catalog (`sync_domain_tables_from_connection`,
  auto-select cap), kicks background indexing, runs `refresh_tools` for tool providers,
  and grants the creator per-DS `manage`.
- `DataSourceService.update_tables_status_delta` (`:3087`) and
  `bulk_update_tables_status` (`:2959`) — table activation.
- Per-agent tool overlay upsert (logic in `routes/data_source_tools.py:147` — to be
  factored into a service method the tool can call; see Gaps).

Training tool machinery to reuse unchanged:

- Registry mode gate: `ToolMetadata.allowed_modes` + `ToolRegistry._matches_filter`
  (`registry.py:184-187`) + runtime re-check in `tool_runner.py:32-45`.
- Runtime ctx gives tools `db / organization / user / report / mode /
  training_build_id` (`agent_v2.py:3514-3545`); training tools scope writes to
  `report.data_sources` (see `create_instruction.py:207-215`).
- Planner: mode-filtered catalog becomes native tool defs; training routing text in
  `prompt_builder_v3.py:99-148`.

---

## RBAC model (decided)

No new permission keys. Reuse the exact grants the HTTP routes already use — but
**enforced in the tool/service layer**, because route decorators do not protect the AI
tool path (the lesson from the training-mode-agent-admins work):

| Action | Required |
| --- | --- |
| See a connection in `list_connections` | Same visibility as `GET /connections` (admin, or a grant on the connection, or it backs an accessible agent). Each row carries `can_create_agent: bool`. |
| "Resolve to" (build on) a connection | org `create_data_source` **and** per-connection `create_data_sources` (implied by `manage_connections` / connection `manage_data_sources` / full admin — `permission_resolver.py:31-62`). |
| `create_agent` | The above, checked per connection id via `PermissionResolver` inside the tool before calling the service. |
| `set_agent_tables` / `set_agent_tools` | Per-DS `manage` on the target agent **and** the agent must be on the current report (training confinement — same rule as instruction scoping). |

Hard rules:

- **No credentials through the LLM.** `create_agent` exposes only Mode 2
  (link existing connections). Mode 1 (inline `type/config/credentials`) and
  `POST /connections` are *never* exposed as tools. Creating a connection stays a
  human/admin UI action.
- **Confinement.** Everything the session mutates is either an agent already on the
  report or an agent created *by this session* (which is attached to the report at
  creation, so the existing `allowed_data_source_ids = report.data_sources` scoping in
  `create_instruction` et al. picks it up automatically).
- Catalog visibility: `create_agent` gets `required_permissions=["create_data_source"]`
  in `ToolMetadata` so non-creators never see the tool; per-connection resource grants
  are checked in-tool (metadata filtering is org-perm only).

---

## The four tools

All: `allowed_modes=["training"]` (not `knowledge` — the auto-suggest harness must not
create agents). Schemas in `backend/app/ai/tools/schemas/`, implementations in
`backend/app/ai/tools/implementations/` (auto-registered).

### 1. `list_connections` — research

Input: `{ connection_id?: str, include_catalog?: bool, search?: str }`

- Summary mode: org connections visible to the actor — `name`, `id`, `type`,
  shape (`tables` | `tools` | `files`), `auth_policy`, table/tool counts, linked agent
  names, `can_create_agent`.
- Detail mode (`connection_id` + `include_catalog`): the connection's catalog so the
  model can plan the selection — tables grouped by schema with column counts (paginated
  / truncated with an explicit "N more" note), or the tool list with descriptions and
  default policies. Backed by `ConnectionTable` / `get_connection_tools`.

This one tool covers both "what can I resolve to" and "what tables/tools does this
connection offer" — no separate catalog tool needed.

### 2. `create_agent` — action

Input: `{ name: str, connection_ids: [str], description?: str, is_public?: bool = false }`
Output: `{ data_source_id, name, connections: [...], seeded_tables: {total, auto_activated}, tools: {total, enabled}, attached_to_report: true }`

- Checks org `create_data_source` + per-connection `create_data_sources`; maps denials
  to a clean `permission_denied` rejection observation (no blind retry), 409 on
  `uq_data_sources_org_name` → `name_taken` with a suggestion.
- Calls `DataSourceService.create_data_source` (Mode 2 only). Creator gets `manage` —
  which is exactly what the follow-up tools and instruction writes require.
- **Attaches the new agent to the current report** (append to `report.data_sources`,
  commit). Consequences, by design:
  - Instruction/eval/prompt tools scope to it immediately (they read
    `report.data_sources` at call time).
  - `set_agent_tables` / `set_agent_tools` confinement admits it.
  - Schema context (ContextHub) includes it from the **next** completion turn;
    `ds_clients` are built at run start, so *querying its data* (`create_data`)
    starts next turn — the observation says so explicitly. Table/tool selection and
    instruction authoring work in the same turn (no client needed).
- `use_llm_sync` defaults **off** in the tool (expensive background onboarding); the
  observation reports the seeded catalog summary instead so the model can proceed to
  selection. `auth_policy="user_required"` connections are created fine, but the
  observation surfaces "users must Connect before tools run".
- License agent cap → clean `limit_reached` rejection (service already raises).

### 3. `set_agent_tables` — action (tables-shaped connections)

Input: `{ data_source_id?: str, activate?: [names], deactivate?: [names],
  action?: "activate_all"|"deactivate_all", filter?: {schema?, connection_id?, search?} }`

- `data_source_id` optional when exactly one agent is on the report (same defaulting
  convention as `create_prompt`).
- Delta path wraps `update_tables_status_delta` (names or ids); bulk path wraps
  `bulk_update_tables_status`. Requires per-DS `manage` + report membership.
- Output: activated/deactivated counts, resulting active-table count, unresolved names
  listed explicitly (never silently dropped).

### 4. `set_agent_tools` — action (tool-shaped connections)

Input: `{ data_source_id?: str, tools: [{name: str, is_enabled?: bool, policy?: "allow"|"confirm"|"deny"}] }`

- Upserts the per-agent `DataSourceConnectionTool` overlay (resolve `ConnectionTool` by
  name across the agent's connections; validate against
  `ToolPolicyService.VALID_TOOL_POLICIES`). Requires per-DS `manage` + report membership.
- Gap to close: the overlay upsert currently lives inline in
  `routes/data_source_tools.py:147-233` — extract a
  `DataSourceService.set_agent_tool_overlay(...)` (or ToolPolicyService method) that
  both the route and the tool call, so the logic and its checks exist once.

**Why two tools, not one `configure_agent`:** tables and tools have disjoint inputs,
disjoint failure modes, and map to different services; separate schemas keep planner
tool-choice and validation crisp. The planner routing text handles shape dispatch.

---

## Planner + frontend

- `prompt_builder_v3.py` training block: add the four tools under **Key tools** with
  routing examples — *"what can I build an agent on?"* → `list_connections`;
  *"create an agent on the Snowflake connection with just the sales schema"* →
  `list_connections(detail)` → `create_agent` → `set_agent_tables`;
  *"enable only the read tools on Monday"* → `set_agent_tools`.
- Frontend tool cards (`frontend/components/tools/`): `ListConnectionsTool.vue`
  (compact table), `CreateAgentTool.vue` (agent card + link to the agent page),
  `SetAgentTablesTool.vue` / `SetAgentToolsTool.vue` (change summary). Register in the
  `tool_name → component` switch in `frontend/pages/reports/[id]/index.vue` (~:1650)
  and `TraceModal.vue`.
- The report header's agent chips must reflect the mid-session attach: emit an SSE
  event (e.g. `report.data_sources_changed`) from `create_agent`'s ToolEnd handling, or
  refetch report data sources when a `create_agent` tool card completes.

---

## Entry gate interplay (explicit non-goal for v1)

Training entry stays as-is (org `enable_training_mode` + full admin / per-DS
`manage_instructions` on every report DS). The new tools appear only for actors who
*additionally* hold `create_data_source`. A "bootstrap" session — entering training on a
report with **zero** agents in order to create the first one — is a follow-up: it needs
an entry-gate change (allow training on an empty report for holders of
`create_data_source`) and is intentionally out of scope here.

## Out of scope / never

- LLM-created **connections** (credentials, config, DCR) — human/admin only.
- Exposing Mode 1 of `create_data_source`.
- Deleting agents or detaching connections from tools.
- Per-user tool policy (`/my_policy`) via tools — org/agent overlay only.

---

## Test plan (mirrors the existing feedback-loop format)

- **Loop A — registry gating (no DB/LLM):** the four tools present in
  `mode="training"`, absent in `chat`/`deep`/`knowledge`.
- **Loop B — tool behavior + RBAC (DB, no LLM, via `run_stream`):**
  - `list_connections`: admin sees all + `can_create_agent=true`; member with only a
    DS-backed view sees the connection with `can_create_agent=false`; detail mode
    returns catalog.
  - `create_agent`: creator-with-grant → success, DS attached to report, creator holds
    `manage`, tables seeded; no org `create_data_source` → `permission_denied`; grant on
    connection A but targeting B → `permission_denied`; duplicate name → `name_taken`;
    license cap → `limit_reached`.
  - `set_agent_tables`: delta + bulk on the created agent; agent **not on the report**
    → rejected (confinement); non-manager → `permission_denied`; unknown table names
    reported.
  - `set_agent_tools`: overlay upsert visible via `GET /data_sources/{id}/tools`
    effective policy; invalid policy rejected.
  - Cross-check: after `create_agent`, `create_instruction` with omitted scope attaches
    to the new agent (not global) — the existing scoping guarantee extends.
- **Loop C — regression:** `test_rbac_data_sources.py`, `test_rbac_training_mode.py`,
  `test_prompt_training_tools.py`, connection/tools route tests — unchanged.
- **Loop D — live UI/LLM (Haiku):** in a training session: "list connections" →
  card; "create an agent on <conn> named X with only schema S" → agent appears in the
  org + on the report chips; "disable the send tools" → overlay reflects in
  ToolsSelector. Confirm a member without `create_data_source` never sees the tools.

## Build order

1. `list_connections` + `create_agent` (incl. report attach + SSE) + planner text.
2. `set_agent_tables` + `set_agent_tools` (incl. overlay-service extraction).
3. Frontend tool cards + chip refresh.
4. (Follow-up) empty-report bootstrap entry gate; optional catalog-preset connection
   creation stays admin-UI-only.
