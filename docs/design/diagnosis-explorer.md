# Plan: Diagnosis explorer — one query language over agent runs and their tool calls

Design canvas: https://claude.ai/code/artifact/6a9f2648-ba59-4306-8a35-f91d2b03bc1b

## Mission

Replace `/monitoring/diagnosis` with a log explorer: one query language over
agent runs and their tool calls, one histogram, one table, everything derived
from the same parsed query. Clean and minimal on screen, strict and fast
underneath. The query language is a product surface: the page is its first
client, an agent tool (`list_agent_runs(q=…)`) is the next.

Principles, in priority order:

1. **One source of truth.** The query string in the URL is the whole page
   state. Chart, table, summary, tools strip, chips and filter builder are all
   views of it. There is no basic/advanced mode.
2. **Fast.** Every predicate and sort hits an indexed column on
   `agent_executions`. `users` and `reports` are joined by primary key for
   display only. Tool predicates are one indexed EXISTS. No joins to
   completions, feedback or usage tables at read time.
3. **Instant.** Parse, highlight, chip state and errors on every keystroke with
   zero network. Fetch only on commit.
4. **Easy, then powerful.** `status:error` is the whole lesson; the "+ Filter"
   builder means most people never type a field name. OR, NOT, ranges and
   grouping are there when needed.
5. **Robust.** One grammar spec, one field registry, golden fixtures shared by
   the Python and TypeScript parsers, errors an LLM can act on.

## What is deliberately not in v1

Each of these is designed for but not built now. None changes the data model.

- Simple terms rendered as editable chips inside the bar (v2 of the builder;
  the canvas shows it). v1 is a text bar plus the builder.
- Saved queries. v1 has the built-in quick filters and the URL.
- Live match count while typing.
- Cost and tokens per tool call (`tool.cost`, `tool.tokens`).
- Fields with a thin case: `models` (every model used), `cost.input/output`,
  `tokens.cached`, `llm_calls`, `feedback.message`, `judge.tool_calls`.
- Server-side result cache. Indexes make it unnecessary at this scale.
- A globally sorted list of tool calls across runs; a group-by UI; a full-text
  engine (bounded `ILIKE` over the time window is v1).
- The `list_agent_runs` tool itself. It becomes a wrapper around one function.

## Decisions from the design review

| Decision | Choice |
|---|---|
| Rows | Agent runs. Each row expands to its tool calls. No runs/tool-calls toggle. |
| Tool-level questions | `tool.*` terms match runs containing such a call; matching rows open on load with the matching calls tinted. A per-tool strip (calls · errors · p50) above the table answers "which tool, how often, how slow". |
| Filters vs. language | Both, one model. Quick-filter chips and the "+ Filter" builder write terms into the query; they light up when the query contains their terms. |
| KPI cards, tabs, dropdowns | Gone. One summary line above the chart; `agent:` and `user:` are fields with facet suggestions. |
| Time | `DateRangePicker` extended mode, default **Last 30 days**, never all-time by default. Clicking a histogram bar inserts a `created:` token. |
| URL | `?q=…&range=…&cursor=…&sort=…` is the page state; links are shareable. |
| Row click | Opens the existing `TraceModal`, unchanged. |

## Query language

KQL-shaped. Whitespace-separated terms, implicit `AND`. Keywords and field
names are case-insensitive.

```
query      := orExpr
orExpr     := andExpr ( "OR" andExpr )*
andExpr    := notExpr ( ("AND")? notExpr )*
notExpr    := ("NOT" | "-")? primary
primary    := "(" orExpr ")" | term
term       := field ":" value                       -- predicate
            | field ":" "(" value ("OR" value)* ")"  -- any-of
            | phrase | word                          -- bare text
value      := comparison | range | phrase | word | wildcard
comparison := (">" | ">=" | "<" | "<=") scalar
range      := scalar ".." scalar
scalar     := number | duration | money | date | word
number     := 12 | 1.5 | 18k | 2.3m
duration   := number ("ms" | "s" | "m" | "h")
money      := "$" number
date       := YYYY-MM-DD | YYYY-MM | today | yesterday | -7d | -24h
phrase     := '"' … '"'
wildcard   := word containing "*"
```

- Bare words and phrases search prompt text, run error and tool error
  (case-insensitive substring).
- `field:value` is equality (case-insensitive for text); `*` wildcards;
  `field:(a OR b)` is any-of; `has:field` is non-null. Negation is `NOT` or `-`.
- Durations accept bare milliseconds; money accepts bare dollars; numbers
  accept `k`/`m`. A date without a time is the whole day in the caller's
  timezone (`tz` param); `-7d` is relative to now.
- **All `tool.*` terms correlate to one tool call** (one `EXISTS`). "Runs
  with a failed create_data call" is `tool:create_data tool.status:error`.
- Unknown field or enum value → error with position and up to three "did you
  mean" suggestions. Same message from both parsers.
- Limits: 1,000 characters, 40 terms, depth 4. The AST carries `"v": 1`.

### Fields — runs

| Field | Type | On `agent_executions` (★ = new column) |
|---|---|---|
| `status` | enum `completed` `error` `in_progress` | `status` |
| `user` | text (name or email) | join `users` |
| `agent` | text (data source name) | `report_data_source_association` via `report_id` |
| `platform` | enum `web` `slack` `teams` `email` `api` `automation` | `platform` ★ |
| `feedback` | enum `positive` `negative` `none` | `feedback_direction` ★ |
| `judge.confidence` (alias `confidence`) | 1–5 | `judge_response_score` ★ |
| `judge.instructions` (alias `coverage`) | 1–5 | `judge_instructions_score` ★ |
| `judge.context` | 1–5 | `judge_context_score` ★ |
| `model` | text | `primary_model_id` ★ (the planner's model) |
| `provider` | enum | `primary_provider` ★ |
| `cost` | money | `total_cost_usd` ★ |
| `tokens` `tokens.in` `tokens.out` | number | `total_tokens` ★ `prompt_tokens` ★ `completion_tokens` ★ |
| `duration` `thinking` `first_token` | duration | existing `*_ms` columns |
| `tools` `tools.failed` | number | `tool_count` ★ `failed_tool_count` ★ |
| `report` | text | join `reports.title` |
| `report_id` `run_id` | text, exact | ids |
| `version` | text | `bow_version` |
| `created` | date | `created_at` |
| `eval` | boolean | `is_eval_run`; `eval:false` is implied unless mentioned |
| `error` | text | `error_text` ★ |
| bare words | text | `prompt_text` ★ (first 2,000 chars), `error_text` ★ |

### Fields — tool calls

| Field | Type | On `tool_executions` |
|---|---|---|
| `tool` | text | `tool_name` |
| `tool.action` | text | `tool_action` |
| `tool.status` | enum | `status` |
| `tool.attempt` | number | `attempt_number` |
| `tool.duration` | duration | `duration_ms` |
| `tool.error` | text | `error_message` |

### Quick filters

Errors `status:error` · Failed queries `tool:create_data tool.status:error` ·
Negative feedback `feedback:negative` · Low confidence `judge.confidence:<3` ·
Low coverage `judge.instructions:<3` · Slow `duration:>30s` · Expensive
`cost:>$0.50` · Retried `tool.attempt:>1`. A chip is active when its terms
are present as a top-level conjunction; clicking an active chip removes them.

## Data

### One rollup function

Sixteen new columns on `agent_executions` (★ above, plus `cost_is_partial`),
all recomputed from their sources by **one** idempotent function:

```
refresh_rollup(db, agent_execution_id)
```

It reads the run's completions (prompt, platform, judge scores), feedback,
tool executions (counts), and usage records (cost, tokens, model, provider),
and writes the row. It is called from four places: run finish, feedback write,
judge-score write, and a usage record written after the run finished. The
backfill script is the same function over all runs, batched by 1,000,
resumable. There is no second write path to keep consistent.

### Cost attribution

`llm_usage_records` gets one new indexed column, `agent_execution_id`, set
from the usage-attribution contextvar (`app.ai.llm.usage_attribution`) in the
same place `report_id` is set today; `UsageLimitContext` already knows the
run. The planner-scope record inside the run gives `primary_model_id` and
`primary_provider`.

What the backfill recovers for existing runs:

| Columns | Source | Accuracy |
|---|---|---|
| prompt/error text, platform, judge scores, feedback, tool counts, planner tokens | tables already linked to the run | exact |
| cost, tokens, model, provider | usage records on the run's `report_id` with `created_at` inside `[started_at, completed_at]` | near-exact: runs on one report are sequential. `cost_is_partial` is set when a record on the report falls outside every run's window or two runs overlap. On instances with usage limits, the `usage_events` ledger keyed by user completion id is the primary source and the window sum a cross-check. |
| cost for runs before 2026-06-24 | none: usage records predate the attribution columns | not recoverable; shown as unknown, not zero |

`TraceModal` today shows a per-report cost total and, only on licensed
instances, a per-turn figure from the quota ledger; elsewhere it falls back to
planner-only tokens. After this change the trace endpoint reads the run row,
so the modal and the page agree everywhere.

### Indexes

`perfidx02_diagnosis_indexes`, same existence-checked pattern as
`perfidx01_hot_path_indexes.py`. On `agent_executions`, each
`(organization_id, <predicate>, created_at)` for: `status`, `user_id`,
`primary_model_id`, `primary_provider`, `feedback_direction`,
`total_cost_usd`, `total_duration_ms`; plus `(organization_id, created_at, id)`
for the range and cursor, and `report_id`. On `tool_executions`:
`(agent_execution_id, tool_name)` and `(tool_name, status)`. On
`llm_usage_records`: `agent_execution_id`. Postgres only, behind a dialect
check: `pg_trgm` GIN on `prompt_text` and `error_text` if the extension is
present.

## Backend

```
backend/app/services/diagnosis/
  grammar.py    tokenizer + recursive-descent parser → AST v1 (pure)
  fields.py     the registry: name, aliases, type, column, enum values,
                facetable, entity, help. Generates GET fields and the tool description.
  compiler.py   AST → SQLAlchemy clause; tool.* → one correlated EXISTS.
                Bound parameters only; LIKE escaped; no text(), no dynamic columns.
  rollup.py     refresh_rollup + backfill
  service.py    run_query and friends
```

`run_query(db, org, scope, q, start, end, tz, cursor, limit, sort, include)`
is the single entry point for the routes and, later, the tool. It always ANDs
organization, the `ConsoleScope` agent subquery, the time range, and
`is_eval_run = false` unless the query mentions `eval:`.

Four endpoints under `/console/diagnosis/`, same gate and scope as today:

| Endpoint | Returns |
|---|---|
| `GET runs` | `{items, next_cursor, summary, histogram, tools}` in one call. `include=items` on cursor pages skips the panels. `summary` and `total_in_range` come from the list statement via `count(*) OVER ()` and conditional aggregates. |
| `GET runs/tool_calls?run_ids=` | `{run_id: [ToolCall]}` for the page's expanded rows (≤ 100 runs) |
| `GET facets/{field}?q=&prefix=` | top 20 `{value, count}` scoped to the range and the rest of the query |
| `GET fields` | the registry, for the builder, suggestions, syntax help and the TS parser check |

A run item: id, created_at, status, prompt (200 chars), user, agents,
platform, model, provider, tools `{total, failed}`, duration_ms, tokens,
cost_usd, cost_is_partial, judge `{confidence, instructions, context}`,
feedback, report_id, completion_id, matched_tool_call_ids. Nothing the table
does not show.

A parse failure returns `400 {code: "bad_query", position, message,
suggestions}`. The client parses first, so this is a backstop and the contract
for the tool.

The old `metrics`, `users`, `timeseries` and `issues/compact` endpoints and
their service methods are removed with the page. `agent_executions/summaries`
stays; other pages use it.

## Frontend

```
frontend/pages/monitoring/diagnosis.vue     thin page
frontend/components/diagnosis/
  QueryBar.vue       <input> over a token-coloured <pre>; suggestions dropdown
                     inside (fields, values with counts, operators; a syntax
                     footer from GET fields). "/" focuses, Enter runs, Tab accepts.
  FilterBuilder.vue  "+ Filter": field list grouped Run / Tool call → values
                     with counts → appends the term and runs.
  QuickFilters.vue   chips; active state derived from the AST.
  ToolsStrip.vue     per-tool pills; click inserts tool:x.
  RunsTable.vue      runs + child tool-call rows; matched rows open on load.
frontend/utils/diagnosisQuery.ts            the TS parser (tokens, AST, errors, cursor context)
frontend/composables/useDiagnosisQuery.ts   URL state, parse, fetch, abort
```

Reused as they are: `DateRangePicker` (extended mode), `DiagnosisActivityChart`
(matched/other series), `TraceModal`.

Columns: Time · Status · Prompt · User · Agent · Tools · Duration · Cost ·
Feedback. Judge scores on hover of the Feedback cell. Fixed columns in v1.

Behaviour: parse on every keystroke, commit on Enter, chip, builder,
histogram click, strip click, time change, or URL load. In-flight requests
are aborted on a new commit; the previous results stay on screen while the
next load. Facets are debounced 150 ms and cached per `(field, prefix,
range)`. Dark mode via the page's existing `dark:` tokens; strings through
`$t('monitoring.diagnosis.*')` with field names and operators untranslated.

## The language as an API

For `list_agent_runs` later: the tool calls `run_query` in-process with the
caller's `security_data_source_ids`, the way `list_agent_executions` does
today; scope is enforced inside. Its description is generated from
`fields.py`, so the LLM reads the same help as the syntax footer. Errors
carry position and suggestions so it can self-correct. `limit` caps at 100
and the time bound cannot be disabled.

## Durability

`backend/tests/fixtures/diagnosis_queries.json` holds golden cases with the
AST version: query → AST, or query → error `{position, message,
suggestions}`. The pytest and vitest suites load the same file and assert
identical output. An e2e test compares the TS field list against
`GET fields`. Adding a field touches `fields.py`, the TS list, and the fixture.

## Performance budget

On a seeded 100k runs / 600k tool calls / 1.5M usage records, 30-day window,
p95, both engines: `runs` ≤ 100 ms including panels, `tool_calls` ≤ 40 ms,
`facets` ≤ 40 ms. `backend/scripts/seed_diagnosis_load.py` seeds and prints
timings for SQLite and Postgres; the numbers go in the PR.

Levers, in order of weight: filter and sort on the anchor's own indexed
columns; always time-bounded (the server rejects a request without a range);
cursor pagination on `(created_at, id)`, no `OFFSET`; correlated EXISTS for
tools; one HTTP call per commit; lean payloads (prompt 200 chars, error 500,
no `result_json`, no `arguments_json`).

## Tests

- Grammar: goldens, limits, every error has a position; `parse(canonical(parse(q))) == parse(q)`.
- Compiler and service on SQLite (extend `seed_agent_executions` with tool
  executions, feedback, judge scores, usage records): one test per field;
  `tool.*` correlation; wildcard and `LIKE` escaping; `eval:` default;
  **scope enforcement** (an agent manager cannot see runs outside their
  agents whatever the query); cursor stability; facets respect `q` and range;
  rollup idempotency and `cost_is_partial`.
- The same service tests on Postgres in CI.
- Frontend unit: parser goldens, chip active state, cursor context → suggestion kind.
- E2E (Playwright, `sandbox-feedback-loop`): default view; `status:` shows
  values with counts; builder adds a term and runs; chip → URL → reload
  restores; `tool.status:error` opens matched rows; row click opens the
  trace modal; partial cost shows `~`.
- Load script timings in the PR.

## Phases

1. **Grammar.** Both parsers, the registry, the golden fixture, `GET fields`
   stubbed from the registry. No UI, no data change. Reviewable as a spec.
2. **Data.** `agent_execution_id` on usage records, the rollup columns,
   `refresh_rollup` and its four call sites, the backfill script, the indexes.
   Additive; nothing reads it yet. Run the backfill on a production-shaped copy
   and report the `cost_is_partial` rate.
3. **Engine and page.** Compiler, `run_query`, the four endpoints, the load
   script; then the components and the page swap; then removal of the dead
   endpoints and the old page's dropdowns, tabs and i18n keys. Split into 3a
   (engine) and 3b (page + cleanup) if the diff gets large.

## Definition of done

- Every field in both tables works end to end and has a test.
- Goldens pass in both parsers; the TS field list matches `GET fields`.
- Load timings under budget on both engines, pasted into the PR.
- Scope test passes; `run_query` enforces scope for in-process callers too.
- No unbounded request; no `OFFSET`; no JSON extraction in a `WHERE`; no read
  joins to completions, feedback or usage tables.
- Old endpoints and page pieces removed; `frontend/tests/i18n` green.

## Open questions

1. Judge scores are written to the user completion from several code paths.
   Phase 2 audits for the one place to call `refresh_rollup`, or the backfill
   sweeps for changed scores nightly.
2. Should `agent:` accept data source ids as well as names? (Names in the
   bar; ids accepted silently for links.)
3. Keep `step_titles` (widget names) anywhere on the page? The design drops
   them; the trace modal still shows them.
