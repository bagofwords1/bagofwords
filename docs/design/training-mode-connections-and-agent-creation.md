# Training mode — list connections, create agent (one-prompt tables/tools)

**Status:** Implemented — see `docs/feedback-loops/training-mode-connections-agent-tools.md`
for the runnable verification loops (registry gating, RBAC tool tests, live LLM + UI).
**Branch:** `claude/training-mode-connections-yodm9m`

Extends **training mode** so the AI can *build* an agent, not just curate one — and so
**one prompt** ("create an agent on the Snowflake connection with just the sales
schema") produces a ready agent:

1. **`list_connections`** (research) — the org connections the training actor can
   build on ("resolve to"). Summary only.
2. **`get_connection`** (research) — one connection's catalog (tables by schema, or
   tools) so the model can plan the selection *before* any agent exists.
3. **`create_agent`** (action) — create the agent linked to existing connection(s),
   with **optional inline table/tool selection**, and attach it to the current
   training session.

Post-create refinement (browsing large catalogs, flipping tool policies) happens **in
the UI on the tool card**, not via chat tools — see "Frontend".

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
- `ConnectionService.get_connection_tools` (`:1684`) and the `ConnectionTable` rows
  (also served by `GET /connections/{id}/tables`, `routes/connection.py:851`) — the
  connection-level catalogs `get_connection` reads. Connection-level is the key
  property: they exist **before** any agent does.
- `DataSourceService.create_data_source` (`data_source_service.py:453`) — **Mode 2**
  (link existing `connection_ids`) already: enforces the license agent cap, seeds
  `DataSourceTable` from the connection catalog (`sync_domain_tables_from_connection`,
  auto-select cap), kicks background indexing, runs `refresh_tools` for tool
  providers, and grants the creator per-DS `manage`.
- `DataSourceService.update_tables_status_delta` (`:3087`) /
  `bulk_update_tables_status` (`:2959`) — applied *internally* by `create_agent` when
  a selection is given (not exposed as standalone tools).
- Per-agent tool overlay upsert (`routes/data_source_tools.py:147`) — same: applied
  internally when a `tools` selection is given (extract a shared service method).

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
| `get_connection` catalog detail | Same visibility rule, checked for the requested connection id. |
| "Resolve to" (build on) a connection | org `create_data_source` **and** per-connection `create_data_sources` (implied by `manage_connections` / connection `manage_data_sources` / full admin — `permission_resolver.py:31-62`). |
| `create_agent` | The above, checked per connection id via `PermissionResolver` inside the tool before calling the service. Inline selection needs nothing extra — the creator receives per-DS `manage` from the service, and selection only ever targets the agent this call just created. |

Hard rules:

- **No credentials through the LLM.** `create_agent` exposes only Mode 2
  (link existing connections). Mode 1 (inline `type/config/credentials`) and
  `POST /connections` are *never* exposed as tools. Creating a connection stays a
  human/admin UI action.
- **No standalone mutation tools.** There is no `set_agent_tables`/`set_agent_tools`
  (see "Alternatives considered") — selection happens only at create time, on the
  just-created agent, so the LLM can never reconfigure an existing agent's catalog.
- **Confinement.** The created agent is attached to the report at creation, so the
  existing `allowed_data_source_ids = report.data_sources` scoping in
  `create_instruction` et al. picks it up automatically.
- Catalog visibility: `create_agent` gets `required_permissions=["create_data_source"]`
  in `ToolMetadata` so non-creators never see the tool; per-connection resource grants
  are checked in-tool (metadata filtering is org-perm only).

---

## The three tools

All: `allowed_modes=["training"]` (not `knowledge` — the auto-suggest harness must not
create agents). Schemas in `backend/app/ai/tools/schemas/`, implementations in
`backend/app/ai/tools/implementations/` (auto-registered).

### 1. `list_connections` — research (summary only)

Input: `{ search?: str }`

Output per connection: `id`, `name`, `type`, shape (`tables` | `tools` | `files`),
`auth_policy`, schema names, table/tool counts, linked agent names,
`can_create_agent`. Deliberately no per-table detail — that is `get_connection`'s job,
keeping this tool always-small.

### 2. `get_connection` — research (catalog detail)

Input: `{ connection_id: str, search?: str, schema?: str, page?: int }`

- Tables-shaped: table names grouped by schema with **column counts only** (not full
  column lists — token budget). Paginated; truncation is explicit ("N more tables —
  filter by schema/search"). Full column detail for a shortlisted table can wait for
  post-create `describe_tables` (which needs the agent on the report anyway).
- Tools-shaped: tool names, descriptions, default `is_enabled`/`policy`.
- Files-shaped: scope summary (glob/root), no per-file listing.

This is what makes **one-prompt creation** possible: the model reads the catalog, then
calls `create_agent` with a concrete selection.

### 3. `create_agent` — action (create + inline selection + attach)

Input:
```
{
  name: str,
  connection_ids: [str],
  description?: str,
  is_public?: bool = false,
  # optional inline selection — applied to the just-created agent:
  schemas?: [str],            # activate all tables in these schemas
  tables?: [str],             # schema-qualified names; union with `schemas`
  tools?: [str]               # enable exactly these; others disabled on the overlay
}
```

Output: `{ data_source_id, name, connections, status, tables: {total, active,
unresolved: [...]}, tools: {total, enabled}, attached_to_report: true }`

Behavior:

- Checks org `create_data_source` + per-connection `create_data_sources`; maps denials
  to a clean `permission_denied` rejection observation (no blind retry); duplicate
  name (`uq_data_sources_org_name`) → `name_taken` with a suggestion; license agent
  cap → `limit_reached`.
- Calls `DataSourceService.create_data_source` (Mode 2 only) — seeds tables with the
  auto-select cap, discovers tools, grants creator `manage`.
- **Selection pass (when given):** deactivate-all then activate the requested
  schemas/tables via the existing bulk + delta service paths; for tools, upsert the
  per-agent overlay. Unresolved names are returned in `unresolved` and stated in the
  observation — never silently dropped. Selection omitted → auto-seeded defaults
  stand, refine in the UI.
- **Attaches the new agent to the current report** (append to `report.data_sources`,
  commit). Instruction/eval/prompt tools scope to it immediately (they read
  `report.data_sources` at call time). Schema context (ContextHub) includes it from
  the **next** completion turn; `ds_clients` are built at run start, so *querying its
  data* (`create_data`) starts next turn — the observation says so explicitly.
- `use_llm_sync` defaults **off** (expensive background onboarding); the observation
  reports the resulting catalog summary instead. `auth_policy="user_required"`
  connections are created fine, but the observation surfaces "users must Connect
  before tools run".

---

## Planner + frontend

Planner (`prompt_builder_v3.py` training block) — Key tools + routing examples:
- *"what can I build an agent on?"* → `list_connections`
- *"create an agent on Snowflake with just the sales schema"* →
  `list_connections` → `get_connection` → `create_agent(schemas=["sales"])`
- *"make a Monday agent with only the read tools"* →
  `get_connection` → `create_agent(tools=[...])`

Frontend tool cards (`frontend/components/tools/`), registered in the
`tool_name → component` switch in `frontend/pages/reports/[id]/index.vue` (~:1650)
and `TraceModal.vue`:

- `ListConnectionsTool.vue` / `GetConnectionTool.vue` — compact tables, collapsed by
  default.
- **`CreateAgentTool.vue` — the centerpiece.** Renders the created agent:
  - **Status** — active/indexing/connection health (`is_active`,
    `last_connection_status`, indexing progress).
  - **Description** — from the tool input / DS row.
  - **Tables list with tabs to Tools and Files** — reuse `AgentKnowledgeTabs.vue`
    (already loads `/data_sources/{id}/connections`, tables, tools, files per agent).
  - **Refinement affordance** — "Edit tables/tools" opens the existing
    `TablesSelector.vue` / `ToolsSelector.vue` in a modal for the new
    `data_source_id` (both are standalone components already used in modals/tabs).
    This is the post-create refinement path; there are no chat tools for it.
  - Link out to the full agent page.
- Report agent chips must reflect the mid-session attach: SSE event (e.g.
  `report.data_sources_changed`) from `create_agent`, or refetch when its card
  completes.

---

## Alternatives considered

1. **Four tools (`set_agent_tables` / `set_agent_tools` as standalone actions)** —
   rejected: duplicates what the UI selectors do better (browsing/pagination/bulk),
   adds mutation tools that could target existing agents (bigger blast radius), and
   more schemas/cards/tests. Kept as a possible follow-up in the narrow form of a
   scoped `update_agent` (report-attached agents only) *if* chat-based refinement
   proves to be demanded.
2. **Two tools only (create, then select purely in UI)** — rejected because it breaks
   the one-prompt goal: without a pre-create catalog view the model cannot honor
   "with just the sales schema", and `create_agent` couldn't validate a selection.
   `get_connection` closes exactly that gap.
3. **Folding catalog detail into `list_connections(include_catalog=...)`** — works,
   but two single-purpose research tools give the planner a crisper choice and keep
   the summary tool guaranteed-small.

---

## Entry gate interplay (explicit non-goal for v1)

Training entry stays as-is (org `enable_training_mode` + full admin / per-DS
`manage_instructions` on every report DS). The new tools appear only for actors who
*additionally* hold `create_data_source`. A "bootstrap" session — entering training on
a report with **zero** agents in order to create the first one — is a follow-up: it
needs an entry-gate change (allow training on an empty report for holders of
`create_data_source`) and is intentionally out of scope here.

## Out of scope / never

- LLM-created **connections** (credentials, config, DCR) — human/admin only.
- Exposing Mode 1 of `create_data_source`.
- Standalone table/tool mutation tools; deleting agents; detaching connections.
- Per-user tool policy (`/my_policy`) via tools — org/agent overlay only.

---

## Test plan (mirrors the existing feedback-loop format)

- **Loop A — registry gating (no DB/LLM):** the three tools present in
  `mode="training"`, absent in `chat`/`deep`/`knowledge`.
- **Loop B — tool behavior + RBAC (DB, no LLM, via `run_stream`):**
  - `list_connections`: admin sees all with `can_create_agent=true`; member with only
    a DS-backed view sees the connection with `can_create_agent=false`.
  - `get_connection`: catalog paginates; schema/search filters; visibility denial for
    an ungranted connection.
  - `create_agent` (no selection): creator-with-grant → success, DS attached to
    report, creator holds `manage`, tables auto-seeded; no org `create_data_source` →
    `permission_denied`; grant on connection A but targeting B → `permission_denied`;
    duplicate name → `name_taken`; license cap → `limit_reached`.
  - `create_agent` (with selection): `schemas=["sales"]` → only sales tables active;
    `tables=[...]` with one bogus name → created, bogus name in `unresolved`;
    `tools=[...]` → overlay reflects exactly the requested set (verify via
    `GET /data_sources/{id}/tools` effective policy).
  - Cross-check: after `create_agent`, `create_instruction` with omitted scope
    attaches to the new agent (not global) — the existing scoping guarantee extends.
- **Loop C — regression:** `test_rbac_data_sources.py`, `test_rbac_training_mode.py`,
  `test_prompt_training_tools.py`, connection/tools route tests — unchanged.
- **Loop D — live UI/LLM (Haiku):** one prompt — *"create an agent on <conn> named X
  with only the sales schema"* → `get_connection` then `create_agent` fire; card shows
  status/description/tables with Tools/Files tabs; "Edit tables" opens the selector;
  agent appears on the report chips and the org agents page. Confirm a member without
  `create_data_source` never sees the tools.

## Build order

1. `list_connections` + `get_connection` + planner text.
2. `create_agent` incl. inline selection, report attach + SSE.
3. `CreateAgentTool.vue` (status / description / tables-tools-files tabs / selector
   modal) + chip refresh + the two research cards.
4. (Follow-up) empty-report bootstrap entry gate; scoped `update_agent` if chat
   refinement is demanded.
