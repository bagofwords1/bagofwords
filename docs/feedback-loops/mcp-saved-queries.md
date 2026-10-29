# Feedback Loop — "Our saved metric definitions don't reach Claude"

Report from an internal user: when people use BOW through Claude over MCP, it
ignores the organization's saved queries and invents its own definitions of
terms the org has already agreed on ("revenue", "active user", "premium
track"). Ask: can saved queries be made available to Claude?

## Root cause (validated)

Saved queries are `Entity` rows (`backend/app/models/entity.py`) — published
metric/model definitions with code, declared parameters and a snapshot. The
in-app planner is shown them as `<entities>` and reuses them with
`describe_entity` / `run_query`. Over MCP neither half existed:

- **No tool.** `MCP_TOOLS` (`app/ai/tools/mcp/__init__.py`) had no entity tool,
  so an external client could not list a saved query or read its code, and
  `get_context` — the "what is here?" call — returned data sources, tables and
  metadata resources only.
- **Not in the generated code's context.** The MCP path builds context with
  `ContextHub.prime_static` (schemas, instructions, resources, files);
  `entity_builder.build_for_turn` runs only in `refresh_warm`, which no MCP
  tool calls (`refresh_warm` callers: `agent_v2.py`,
  `completion_feedback_service.py`). So `create_data`/`inspect_data` generated
  SQL with no knowledge the definition existed.
- **Parameters never populated.** `EntityContextBuilder.build()` built
  `EntityItem` without `parameters`, so the `<parameters>` block
  `EntitiesSection` renders (and `describe_entity`'s own docs point at) was
  dead everywhere — in-app included.

## The change

`list_entities` + `describe_entity` MCP tools, published saved queries in
`get_context`, `ContextHub.prime_entities()` wired into `build_rich_context`,
and the builder now carries declared parameters.

## Loop A — deterministic

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/e2e/test_mcp_entities.py tests/e2e/test_mcp.py \
  tests/e2e/test_entity_context_discovery.py -m e2e --db=sqlite -q
```

`tests/e2e/test_mcp_entities.py` (9 tests) covers discovery, every handle a
saved query goes by, drafts staying invisible, agent-access scoping for a
non-admin member, `get_context`, and `tools/list`. All 9 fail on `main`
(the surface does not exist). `test_discovered_entities_carry_their_declared
_parameters` fails without the builder fix with `assert [] == ['genre']`.

## Loop B — live stack

Backend booted per this skill (sqlite, pinned `BOW_ENCRYPTION_KEY`), chinook
demo agent, one published saved query whose definition nothing would guess:
premium minutes = `sum(Milliseconds)/60000` over `Milliseconds > min_ms`,
`min_ms` defaulting to 400000. Driven against the real `/api/mcp` JSON-RPC
endpoint with an API key, as Claude would
(`scratchpad/mcp_entities_loop.py`):

| call | result |
|---|---|
| `tools/list` | includes `list_entities`, `describe_entity` |
| `get_context` | lists the saved query + `entities_hint` |
| `list_entities` | the saved query with its `min_ms` parameter |
| `describe_entity` | returns the saved code verbatim |
| `describe_entity(run=true)` | `premium_minutes = 10677.36`, `applied_params {min_ms: 400000}`, `data_source: executed` |
| `describe_entity(params={min_ms: 100000})` | `22921.29`, `applied_params {min_ms: 100000}` — saved code, no code generation |

The coder's half was checked in-process against the same rows
(`build_rich_context` → `build_codegen_context`): `entities_context` is now a
populated `<entities count="3">` block with titles, descriptions and
`<param name="min_ms" …>` where it was empty before. The block carries no code
by design (`entities_section.py`: "Code is NOT included in prompt — use
describe_entity"), which is the same contract the in-app planner has.

**Not covered:** `create_data` with a live model, to watch generated SQL reuse
the definition. The sandbox's Anthropic key returned
`credit balance is too low`, so every `create_data` call failed before
codegen. The context it would have been given was verified directly instead.
