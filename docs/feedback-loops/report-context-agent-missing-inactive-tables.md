# Feedback Loop — "the report does not have the right context, even though the prompt box has it"

An agent (data source) is selected in the prompt box and attached to the report,
its connection is live, the user is an **admin** — yet the model answers that it
is connected to *fewer* data sources than are attached, silently omitting one or
more. Mentioning the missing agent with `@` makes it work. This loop reproduces
the failure end-to-end (deterministic + real-LLM + UI) and pins the root cause.

## Root cause (confirmed, reproduced)

**A newly created agent indexes its tables but activates none of them, and a
data source that renders zero tables is silently dropped from the agent's
schema context.** It is not connection health, not RBAC, not connector type.

1. `create_data_source` seeds the agent's tables via
   `sync_domain_tables_from_connection(max_auto_select=self.ONBOARDING_MAX_TABLES)`
   (`backend/app/services/data_source_service.py:726-729`) and
   **`ONBOARDING_MAX_TABLES = 0`** (`:3369`). With `max_auto_select == 0` the
   activation logic (`:4313-4318`) computes
   `should_activate = total_tables <= 0` → **False for every table**, and the
   smart-select fallback `if needs_smart_selection and max_auto_select:` (`:4451`)
   is skipped because `0` is falsy. The agent ends up with **0 active tables**
   (`DataSourceTable.is_active = False` for all).

2. When a completion runs, `SchemaContextBuilder.build(active_only=True)`
   (`backend/app/ai/context/builders/schema_context_builder.py:86-87`) returns
   that data source with an **empty `tables` list**.

3. `TablesSchemaContext.render_combined` then **omits the entire
   `<data_source>` element** when it renders no tables, index, or MCP tools
   (`backend/app/ai/context/sections/tables_schema_section.py:582-583`):

   ```python
   if not (sample_xml or index_xml or mcp_xml):
       continue          # the whole <data_source> is dropped from the prompt
   ```

   The model never sees the agent → "you are connected to N data sources"
   under-counts, with **no signal** that an attached agent was excluded.

`@mention` works because it routes through `MentionContextBuilder` → the
`<mentions>` section (`mentions_section.py`), which force-includes the named
source/tables and bypasses both `report.data_sources` and the `active_only`
table filter — so the model learns the table names and can query the live DB.

Why demo agents work and custom ones don't: the demo installer ships its data
sources with tables **pre-activated** (Music Store: 11/11, Financial Market
Agent: 17/17), so they always render. A freshly created custom agent starts at
0/N until a human opens its tables page and activates tables.

> Ruled out during the investigation: the `reliability_status = "training"`
> badge (all three agents show it — it does not gate context); connection health
> (`Connection.is_active` was `1`); RBAC (admin has full bypass in
> `user_can_access_data_source`). A *separate*, real secondary path — an
> unhealthy connection producing no client, dropped by `AgentV2._has_client` —
> is covered by `tests/unit/test_report_context_datasource_dropped.py`, but it
> is NOT what fires here.

## Loop A — deterministic reproduction (no external services)

`backend/tests/unit/test_report_context_inactive_tables_omitted.py` pins the
render-layer omission:

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
uv run python -m pytest tests/unit/test_report_context_inactive_tables_omitted.py -p no:warnings -q
# 2 passed — an attached agent with zero active tables is absent from render_combined
```

## Loop B — full stack, real data, real LLM (reproduced)

Booted the whole app (`tools/agent/boot_stack.sh --dev`), seeded an org + admin
(`tools/agent/seed_org.py --demo`), configured Anthropic Haiku, and built three
agents across two connector types:

| Agent | Connector | Connection `is_active` | Active tables |
|---|---|---|---|
| Music Store (demo) | sqlite | 1 | **11 / 11** |
| Financial Market Agent (demo) | duckdb | 1 | **17 / 17** |
| Sales Agent (custom, admin-created) | sqlite | 1 | **0 / 11** |

A report was created attached to **all three** (`GET /api/reports/{id}` →
`data_sources: ['Sales Agent','Financial Market Agent','Music Store']`), and the
prompt box shows all three under Auto.

**Direct context check** (`SchemaContextBuilder` + `render_combined` over the real
report):

```
Attached agents:  ['Music Store', 'Financial Market Agent', 'Sales Agent']
rendered_tables:  Music Store=11   Financial Market Agent=17   Sales Agent=0
in prompt?:       Music Store=True Financial Market Agent=True Sales Agent=False
```

**Real Haiku completion** — prompt: *"List EVERY data source / agent you
currently have access to…"*:

> "You have access to **2 data sources**, both published and live: 1. Financial
> Market Agent (DuckDB) … 2. Music Store (SQLite) … There are no agents listed
> separately."

Sales Agent is absent, exactly as predicted. UI evidence:
- `assets/report-context-answer-2-of-3.png` — the answer listing only 2 of 3.
- `assets/report-context-promptbox-3-agents.png` — the prompt-box selector
  showing all 3 agents (Auto ✓; Music Store, Financial Market Agent, Sales Agent,
  all badged "Training").

**The flip** — activating Sales Agent's tables
(`POST /api/data_sources/{id}/bulk_update_tables {"action":"activate"}` → 11
activated) and rebuilding:

```
rendered_tables:  Sales Agent=11
in prompt?:       Sales Agent=True
```

The agent reappears in the context the moment its tables are active.

## Proposed fix (NOT applied — root-cause report only)

The defect is a **silent** omission of an attached-but-unactivated agent. Options,
at the two seams:

1. **Don't silently drop an attached source.** In `render_combined`
   (`tables_schema_section.py:582`), when a `report.data_sources` member renders
   zero tables, still emit a minimal `<data_source>` with a status note
   ("no tables activated yet — select tables on the agent page") instead of
   `continue`. The model then reports the agent honestly rather than denying it.
2. **Make new agents usable by default.** `ONBOARDING_MAX_TABLES = 0` means every
   new agent is dead-on-arrival for the automatic path until a human activates
   tables. A small positive default (the docstring at `:4262` still says `20`)
   or auto-activating when `total_tables` is small would remove the trap. If the
   zero-default is intentional onboarding, the prompt box should not present an
   agent with 0 active tables as ready-to-use without a warning.
3. **Surface it in the UI.** The selector shows the agent as selectable with no
   hint that it will contribute nothing; a "0 tables active" badge would close
   the gap between what the prompt box shows and what the agent receives.

## What this proves / regression notes

The automatic schema context (`report.data_sources` → `SchemaContextBuilder` →
`render_combined`) and what the prompt box shows are governed independently, and
an attached agent with zero active tables is dropped from the former with no
signal. Loop A asserts the general invariant at the render layer; Loop B confirms
it against the real stack and a live model. The test asserts the invariant (empty
source ⇒ omitted), not one scripted agent, so it survives as a regression guard
for whichever fix reconciles the two.
