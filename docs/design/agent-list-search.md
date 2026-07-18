# Agent list / search — roster + focus for many-agent reports

**Status:** Implemented. **Branch:** `claude/agent-list-search-tools-ctr7d7`

## Problem

"Agent" is the user-facing name for a `DataSource`. When a report is attached to
many agents (Auto mode selects them all), every agent's full schema (tables /
columns or MCP tools) plus its always-loaded instructions is dumped into the
planner's user message via `schemas_combined`. Dozens of agents ⇒ the context
blows up, and the model has been observed to under-count / lose agents it can't
fully see (`docs/feedback-loops/report-context-agent-missing-inactive-tables.md`).

## Design

Progressive disclosure, gated on agent count. This is the same pattern the repo
already uses for **instructions** (`<available_instructions>` index +
`read_instruction`/`search_instructions`) and **MCP resources**, applied to the
one heavy context block that was still eagerly, fully rendered: agent schema.

- **Roster (always):** a thin `<available_agents>` block listing **every**
  attached agent — `name`, one-liner, item count, `published`/`draft`, and which
  are `focused`. The one-liner is `description` → primary-instruction snippet →
  `context` (fallback chain). This is the undercount guard: the model always
  knows what exists; only heavy schema is deferred.
- **Focus:** the subset of agents whose FULL schema is rendered. Resolved each
  planner turn as:
  1. explicit `report.focused_data_source_ids` (set via `set_report_agents` or
     the prompt-box focus selector), else
  2. when attached agents > `BOW_AGENT_INDEX_THRESHOLD` (default 4): auto-seed
     the top `BOW_AGENT_FOCUS_SEED` (default 3) by **per-user** recent usage,
     else
  3. few agents → render everything (behavior identical to before this feature).
- **Two tools** (`chat`/`deep`/`training`):
  - `search_agents` (research): match `query` terms against name / description /
    primary instruction / table names, rank by the caller's usage, and return the
    matched agents' **full tables/tools schema + always-on instructions** in the
    observation — i.e. what an attached agent looks like today. Omit `query` to
    list candidates.
  - `set_report_agents` (action): set `report.focused_data_source_ids` (and
    attach an agent not yet on the report so it renders). Empty list clears focus.
- **Mode scope (requirement):** in **training** mode both tools operate only on
  agents the user can **manage** (`get_ds_ids_with_permission(..., "manage_instructions")`);
  in chat/deep they operate on the report's attached agents.
- **UI:** a compact prompt-box chip mirroring the LLM model selector
  (`AgentFocusSelector.vue`) — "Auto" or the focused agent(s); dropdown lists
  attached agents with checkmarks; persists via `PUT /reports/{id}
  {focused_data_source_ids}`. Shown only when ≥2 agents are attached.

Focus trims the **planner's** context only; `create_data`'s coder still
introspects any attached agent, so focusing never removes access — it just saves
tokens and steers attention.

## Key files

- `backend/app/models/report.py` — `focused_data_source_ids` (JSON) + migration
  `alembic/versions/agnt1focus01_...`.
- `backend/app/ai/context/agent_roster.py` — roster render, per-user usage
  ranking, one-liner fallback, `build_focus_and_roster`.
- `backend/app/ai/agent_v2.py` — `_render_schemas_with_roster` wired into both
  planner-assembly seams; `PlannerInput.agents_roster` rendered in
  `prompt_builder_v3.py` before `schemas_combined`.
- `backend/app/ai/tools/{schemas,implementations}/search_agents.py`,
  `set_report_agents.py`, `implementations/agent_focus_common.py`.
- `backend/app/schemas/report_schema.py`, `services/report_service.py` — write +
  read of the focus field.
- `frontend/components/prompt/AgentFocusSelector.vue` + `PromptBoxV2.vue`.

## Verification (real LLM — Claude Haiku 4.5)

- 21 agents attached, no explicit focus → roster mode; context **21,013 →
  5,324 chars (75% reduction)**, all 21 agents listed, 3 focused, Music Store's
  description one-liner shown.
- "Which genres generate the most revenue?" → model called
  `set_report_agents` (focus Music Store) → `create_data` (bar chart); focus
  persisted to the DB and hydrated into the prompt-box chip.
- `search_agents(["music","album","invoice"])` → returned Music Store with its
  full `<data_source>` schema (tables + columns) in the observation.
- Unit tests: `tests/unit/test_agent_roster.py`, `tests/unit/test_agent_focus_scope.py`.

## Open / follow-ups

- Mid-run attach of a brand-new agent (training) renders its full schema on the
  next message (the run-start schema cache doesn't include it) — consistent with
  `create_agent`'s "querying starts next message".
- Per-user usage currently derives from report attachment recency; a dedicated
  `DataSourceUsageEvent` (mirroring `InstructionUsageEvent`) would sharpen
  ranking. Instructions are still loaded for all attached agents (not scoped to
  focus) to preserve always-on rules.
