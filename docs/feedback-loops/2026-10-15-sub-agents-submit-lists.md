# Feedback Loop — "can submit list records run in separate sub agent runs?"

**Status: IMPLEMENTED and VERIFIED (2026-10-15).** Loop A passes on SQLite; Loop B ran live with
**Claude Haiku 5.5**: the parent delegated, a `validate` child wrote three rows into the parent's list
through `submit_top_customers`, and finished with the saved count.

Before this change a child could never write into an Agent List: `submit_list` sat in
`BLOCKED_IN_SUB_AGENTS`, so every `submit_<slug>` call was rewritten to the gateway and refused at
dispatch ("not available to a sub-agent"). Worse, the child's catalog still advertised the
`submit_<slug>` tools, because the per-list registration ran after the profile narrowing — a child
could burn a step on a call the loop would refuse.

## Root cause (validated)

- `backend/app/ai/sub_agent_profiles.py` — `submit_list` in `BLOCKED_IN_SUB_AGENTS`, so no profile
  could ever hold it.
- `backend/app/ai/agent_v2.py` — `_bind_sub_agent()` (profile narrowing, run setup) precedes
  `_register_list_tools()` (appends `submit_<slug>` per list), and the dispatch check compares the
  **rewritten** name (`submit_list`) against the profile. Net effect: offered in the catalog, refused on call.

## The change

| Piece | Where |
|---|---|
| `submit_list` leaves the global block; `create_list` / `edit_list` join it. `validate` always holds `submit_list`; `investigate` holds it with `create_data` (org switch `sub_agent_allow_create_data`). Profile addendum tells a child to save extracted rows with the tool. | `backend/app/ai/sub_agent_profiles.py` |
| A child's catalog only gets the `submit_<slug>` tools when its profile holds `submit_list` (no more advertised-but-refused calls). | `backend/app/ai/agent_v2.py` `_register_list_tools` |
| Child system prompt: a LISTS line (save extracted records with `submit_<list>`, same key updates, never invent a list). | `backend/app/ai/agents/planner/prompt_builder_v3.py` |
| `delegate.profile` description names the extraction fan-out use of `validate`. | `backend/app/ai/tools/schemas/sub_agents.py` |
| Design notes | `docs/design/sub-agents.md` A6, `docs/design/structured-extraction.md` §7 |

Not changed, and checked: permissions (a child runs as the parent's user, so `can_submit_to_list`
applies unchanged); provenance (rows carry `report_id` and the child's tool execution); the same-key
race between parallel children (unique `(list_id, key_value)` index + retry-as-update in
`apply_submission_with_retry`, covered by `test_concurrent_submissions_of_the_same_new_key_yield_one_row`).

## Loop A — deterministic (scripted LLM, no provider)

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_sub_agent_list_submit.py tests/unit/test_sub_agent_prompts.py -q --db=sqlite
```

Observed: `11 passed in 26s`.

- `test_validate_child_submits_rows_into_the_parents_list` — the child's planner call offers
  `submit_contracts` and none of `create_list` / `edit_list` / `delegate` / `clarify`; the child's calls are
  `[submit_list, finish_subtask]`; one row exists, keyed `acme ltd`, with the report as provenance;
  the child's record summary carries the saved count.
- `test_investigate_child_without_create_data_never_sees_the_list_tool` — with the org default
  (`sub_agent_allow_create_data` off) no child planner call offers a `submit_*` tool; the child finishes
  `blocked`; the list stays empty.
- `test_profiles_never_include_blocked_or_parent_only_tools` (unit) — `submit_list` in `validate`,
  in `investigate` iff `create_data`; schema tools never.

Regression: `tests/e2e/test_sub_agents.py` (wave fan-out / fan-in, stop, limits) and
`tests/e2e/rbac/test_list_schema_tools.py` — observed `25 passed in 120.84s` on SQLite.

## Loop B — live, Claude Haiku 5.5

Same sandbox as `2026-10-15-create-list-edit-list-training-tools.md` (Music Store agent, the
`Top Customers` list created there). Chat-mode report; prompt:

> Delegate this to one background sub-agent with the 'validate' profile and end your turn: find the 3
> customers from Germany with the highest total invoice amount (…) and save them as rows into the
> 'Top Customers' list using its submit tool. Report the saved row count.

Observed (`GET /reports/{id}/sub-agents`, the child's detail, the list rows, `backend.log`):

```
WAVE done total 1
CHILD Saving top German customers | validate | finished finish_call done
  calls: [create_data, create_data, describe_tables, inspect_data, create_data, create_data, submit_list, finish_subtask]
  summary: Top 3 German customers by total invoice amount were saved to the 'Top Customers' list: 3 rows inserted. …
rows total 8 | from the delegate report: 3
   fynn zimmermann  country=Germany total_spent=43.62 invoice_count=7
   leonie köhler    country=Germany total_spent=37.62 invoice_count=7
   hannah schneider country=Germany total_spent=37.62 invoice_count=7
child planner tools: [... 'finish_subtask', 'inspect_data', ..., 'submit_top_customers', 'write_csv']
has submit_top_customers: True | has create_list: False | has delegate: False
[agent] registered 1 list tool(s): submit_top_customers      <- the child's own run
```

## What this proves / regression notes

A parent can fan extraction out to children that write typed rows themselves, under the same
permission and upsert rules as the parent, and a child whose profile lacks the switch never sees the
tool. Not covered here: the Postgres leg of Loop A (CI runs it); a multi-child same-key race under a
live model (covered deterministically by the existing concurrency test).
