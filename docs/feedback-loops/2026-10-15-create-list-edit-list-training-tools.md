# Feedback Loop — "do we have a create list schema tool? besides only submit?"

**Status: IMPLEMENTED and VERIFIED (2026-10-15).** Loop A passes on SQLite; Loop B ran live
with **Claude Haiku 5.5** through the real UI (create, approve, hot-registered submit, edit).

Before this change the only list-related agent tool was the `submit_<slug>` gateway: a list's
schema could be created or edited only by a person in the Knowledge Explorer. The claim validated
here: in **training mode** the agent can propose a new list (`create_list`) or a schema change to an
existing one (`edit_list`), the user approves it on a chat card, the list is saved live, and the
`submit_<slug>` tool for it is usable **in the same session**.

## Root cause (validated)

Not a bug — a missing capability. `backend/app/ai/tools/list_tool_registry.py` registers one
`submit_<slug>` per existing list, once per run (`AgentV2._register_list_tools`, called from the run
setup at `agent_v2.py:4904`); nothing in `app/ai/tools/implementations/` wrapped
`services/agent_lists/service.py:create_list` / `update_list`, which only the REST routes in
`routes/agent_lists.py` reached.

## The change

| Piece | Where |
|---|---|
| Tool schemas | `backend/app/ai/tools/schemas/create_list.py`, `edit_list.py` |
| Tools (training-only, MANAGE on the owning agent, pause on `stream_user_confirmation`) | `backend/app/ai/tools/implementations/create_list.py`, `edit_list.py`, `_list_tool_common.py` |
| Hot-registration after approval (`runtime_ctx["lists_changed"]` → `_register_list_tools(refresh=True)`) | `backend/app/ai/agent_v2.py` |
| Training prompt guidance ("create a list" means `create_list`) | `backend/app/ai/agents/planner/prompt_builder_v3.py` |
| Chat card (review / approve / request changes, per-field diff, breaking warning, he/es/en) | `frontend/components/tools/ListSchemaTool.vue`, mapped in `pages/reports/[id]/index.vue` and `pages/c/[token]/index.vue`; `locales/{en,es,he}.json` `tools.listSchema.*` |
| Design note | `docs/design/structured-extraction.md` §7.1 / §7.2 |

The schema goes through the same `ListSchemaIn` contract as `POST/PUT /data_sources/{id}/lists`, so
the tool can never create a list the editor would reject. Edits keep field ids (renames are not
removals) and are classified with `classify_schema_change`; a breaking edit bumps the version exactly
like the editor.

## Loop A — deterministic (no external services)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_list_schema_tools.py -q --db=sqlite
```

Observed: `20 passed in 64s`. The tests drive the tools exactly as AgentV2 does (shared session +
session maker, `mode="training"`), answer the review card through `ToolConfirmationService.resolve`,
and assert through the public list API and `build_list_tools`:

- training-only catalog; viewer refused before any card; invalid schema → `invalid` + path errors, no card;
- decline → nothing written, feedback returned; approve → list live, `submit_<slug>` offered by the next
  registration pass with the compiled schema, `lists_changed` flagged;
- duplicate name / list cap refused before review; two agents → `needs_agent` with choices; a manager of
  one agent cannot create on the other;
- edit: resolve by name, additive edit keeps version + field ids (rename), breaking edit (field removal)
  bumps the version and the card says so; declined edit changes nothing; no-op and bad field refs → no card.


## Loop B — live, Claude Haiku 5.5, real UI

```bash
tools/agent/boot_stack.sh                 # backend :8000 + production frontend :3000
cd backend && uv run python ../tools/agent/seed_org.py --demo     # admin + Music Store agent
# API (secrets via env only): enable_training_mode on; Anthropic provider with
# claude-haiku-5-5 as default + small default (POST /api/llm/providers, /set_default).
# Training report on Music Store; POST /api/reports/{id}/completions?background=true with
# prompt.mode="training" (the background API honors only an explicit prompt.mode).
cd frontend && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers \
  node tests/lists/list-schema-flow.mjs <report_id> ../media/pr/ai-focused-hawking-u35duj/haiku
```

Prompt: *"Create a list called 'Top Customers' on this agent with these fields: customer_name (text,
the key), country (text), total_spent (number, USD) and invoice_count (whole number). Once it exists,
fill it with the 5 customers who spent the most in total across their invoices."*

Observed (tool executions of the run, via `GET /reports/{id}/completions`, and `backend.log`):

```
- create_data   success
- create_list   success  {'status': 'created', 'list_name': 'Top Customers', 'tool_name': 'submit_top_customers'}
  [agent] refreshed 1 list tool(s): submit_top_customers          <- planner dump: 54 tools -> 55
- submit_list   success  {'inserted': 5, 'updated': 0}            <- the hot-registered tool, same run
LIST 'Top Customers' slug=top_customers tool=submit_top_customers v1
     fields=['customer_name','country','total_spent','invoice_count'] key=customer_name rows=5
```

Second turn: *"Please add a 'city' text field … then re-fill it"* →

```
- edit_list     success  {'status': 'updated', 'change': 'additive'}   (version stays 1)
- submit_list   success  {'inserted': 0, 'updated': 5}                 (rows now carry city)
```

Evidence (`media/pr/ai-focused-hawking-u35duj/`): `haiku/01-review-card.png` (proposal),
`haiku/02-created-card.png` (saved, with the submit tool + `bow.…` table), `haiku/04-submit-card.png`
(five rows written by the hot-registered tool), `haiku-edit/05-card-he.png` (RTL),
`haiku-edit/06-card-mobile.png`.

### Things learned on the way (all environmental, none in the change)

- The first API key had no credit (`credit balance is too low`); the second worked.
- Restarting the backend without `BOW_ENCRYPTION_KEY` makes stored provider credentials
  undecryptable — set a stable Fernet key before restarting a sandbox.
- `POST …/completions?background=true` runs in **chat** mode unless `prompt.mode` is sent; the
  planner dump (`BOW_PLANNER_DUMP_FILE`) is the ground truth for what the model was offered.
- Replaying a dumped request directly against Anthropic showed Haiku calling `create_list`; inside the
  app (parallel tool use disabled) it once picked `search_mcps` first and then declared the tool
  missing. The training prompt now says explicitly that "create a list" means `create_list`; the next
  three runs all called it first.
- A review card expires after 240 s; a poller must watch the `tool_confirmations` table (the
  completions API does not expose blocks of an in-progress follow-up turn).

## What this proves / regression notes

An agent manager in training mode can define and reshape a list from chat with one approval, and
extract into it immediately; viewers and non-managers cannot; nothing is written without approval.
Not covered here: Postgres leg of Loop A (CI runs it), the shared `/c/<token>` readonly rendering
(mapped, not screenshotted).
