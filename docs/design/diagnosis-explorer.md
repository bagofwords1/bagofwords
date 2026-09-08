# Plan: Diagnosis explorer — one query language over agent runs and their tool calls

Design canvas: https://claude.ai/code/artifact/6a9f2648-ba59-4306-8a35-f91d2b03bc1b
(the "Agent runs" board is the target; the "Tool calls" board is superseded by
expandable rows + the tools strip decided below).

## Mission

Replace `/monitoring/diagnosis` with a log-explorer page: one query language
over agent runs and their tool calls, one histogram, one table, everything
derived from the same parsed query. Clean and minimal on screen, strict and
fast underneath. The query language is a product surface in its own right: the
page is its first client, an agent tool (`list_agent_runs(q=…)`) is the next.

Principles, in priority order:

1. **One source of truth.** The query string in the URL is the whole page
   state. Chart, table, summary line, tools strip and facets are projections of
   it. No second filter system, ever.
2. **Blazing fast.** Every list/histogram/facet query anchors on
   `agent_executions`, filters and sorts on its own indexed columns, joins
   `users`/`reports` by primary key only for display, and uses one indexed
   EXISTS for tool predicates. No joins to completions, feedback or usage
   tables at read time. Sub-100 ms on 100k runs on both SQLite and Postgres.
3. **Instant feedback.** Parsing, highlighting, chip state and error messages
   happen on every keystroke with zero network. A count preview arrives while
   you type. Enter never waits on anything the client could have known.
4. **Easy, then powerful.** `status:error` is the whole lesson for a new user;
   a bare word searches text. Operators, ranges, grouping and `tool.*`
   correlation are there when needed and never required.
5. **Robust.** One grammar spec, a versioned AST, golden fixtures shared by the
   Python and TypeScript parsers, an explicit field allowlist that also
   generates the docs, and error messages good enough for an LLM to self-correct.

## Non-goals (v1)

- A globally sorted/paged list of tool calls across runs (the tools strip
  covers the aggregate question; a drill-down can be added later).
- Group-by UI beyond the histogram and the per-tool strip.
- A full-text engine. Bounded `ILIKE` over a 30-day window is v1; the upgrade
  path is noted under Performance.
- The `list_agent_runs` tool itself. This plan makes it a thin wrapper; it is
  built separately.
- Touching `/monitoring` (Explore) or `/monitoring/cost`.

## Decisions carried over from the design review

| Decision | Choice |
|---|---|
| Entity model | Agent runs are the rows. Each row expands to its tool calls. No runs/tool-calls toggle. |
| Tool-level questions | `tool.*` fields match runs containing such a call; matching calls are highlighted and auto-expanded. A per-tool strip (calls · error % · p50) above the table answers aggregate questions. |
| Old issue tabs | Quick-filter chips that insert tokens into the query. They light up when the query contains their tokens. |
| KPI cards | Removed. One summary line above the chart, computed from the current query. |
| Agent / user dropdowns | Removed from this page. `agent:` and `user:` fields with facet autocomplete replace them. Security scope (agent managers) is unchanged and server-side. |
| Time | Picker with presets + custom; default **Last 30 days**, never all-time by default. Clicking a histogram bar inserts a `created:` token. |
| URL | `?q=…&range=30d&cursor=…&sort=…` is the page state; links are shareable. |
| Row click | Opens the existing `TraceModal` (`report_id` + `completion_id`), unchanged. |

## Query language

KQL-shaped. Whitespace-separated terms, implicit `AND`. Case-insensitive
keywords and field names.

```
query      := orExpr
orExpr     := andExpr ( "OR" andExpr )*
andExpr    := notExpr ( ("AND")? notExpr )*
notExpr    := ("NOT" | "-")? primary
primary    := "(" orExpr ")" | term
term       := field ":" value                      -- predicate
            | field ":" "(" value ("OR" value)* ")" -- any-of
            | phrase | word                         -- bare text (full-text)
value      := comparison | range | phrase | word | wildcard
comparison := (">" | ">=" | "<" | "<=" | "!=") scalar
range      := scalar ".." scalar
scalar     := number | duration | money | date | word
number     := 12 | 1.5 | 18k | 2.3m                  -- k/m suffixes
duration   := number ("ms" | "s" | "m" | "h")
money      := "$" number                            -- USD
date       := YYYY-MM-DD | YYYY-MM | today | yesterday | -7d | -24h
phrase     := '"' … '"'
wildcard   := word containing "*"
```

Semantics:

- Bare words and phrases search `prompt`, run `error` and tool `error` text
  (case-insensitive substring). A phrase is one substring.
- `field:value` on enum/text fields is equality (case-insensitive for text);
  `field:*sql*` is a wildcard; `field:(a OR b)` is any-of; `field:!=x` negates.
- Comparisons and ranges work on number, duration, money and date fields.
  Durations accept bare milliseconds (`duration:>30000`); money accepts bare
  dollars (`cost:>0.5` ≡ `cost:>$0.5`); numbers accept `k`/`m` (`tokens:>50k`).
- `has:<field>` means non-null. `is:error`, `is:slow`, `is:retried`,
  `is:negative` are sugar defined in the field registry (each expands to a
  normal predicate; the expansion is visible in the syntax help).
- A date without a time means the whole day in the caller's timezone (`tz`
  param); `created:2025-08` is the whole month; `-7d` is relative to now.
- **`tool.*` clauses correlate to one tool call.** All `tool.*` terms in the
  query must match the same `tool_executions` row (one correlated `EXISTS`).
  That is what "runs with a failed create_data call" means. Documented in the
  syntax help; no cleverer semantics in v1.
- Unknown field → error with position and up to three "did you mean"
  suggestions (edit distance over the registry). Unknown enum value → error
  listing the values. Both are caught client-side before a request is sent and
  returned by the server identically.
- Limits: 1,000 characters, 40 terms, nesting depth 4. Enforced in both parsers.

The AST is versioned (`{"v": 1, …}`). Anything that consumes it (page, tool,
tests) pins the version; the fixture file records it.

### Fields — agent runs

| Field | Type | Column on `agent_executions` (★ = new) | Notes |
|---|---|---|---|
| `status` | enum | `status` | `completed` `error` `in_progress` |
| `user` | text | join `users` (name, email) | facet |
| `agent` | text | `report_data_source_association` via `report_id` (exact) | facet; multi-agent runs match any |
| `platform` | enum | `platform` ★ | `web` `slack` `teams` `email` `api` `automation` |
| `feedback` | enum | `feedback_direction` ★ | `positive` `negative` `none` |
| `feedback.message` | text | `feedback_message` ★ | substring |
| `judge.confidence` (alias `confidence`) | number 1–5 | `judge_response_score` ★ | `response_score` on the user completion |
| `judge.instructions` (alias `coverage`) | number 1–5 | `judge_instructions_score` ★ | `instructions_effectiveness` |
| `judge.context` | number 1–5 | `judge_context_score` ★ | `context_effectiveness` |
| `judge.tool_calls` | number 0–1 | `judge_tool_call_score` ★ | from `tool_call_judge`, if recorded |
| `model` | text | `primary_model_id` ★ | facet; the planner's model for the run |
| `provider` | enum | `primary_provider` ★ | facet: `openai` `anthropic` `azure` … |
| `models` | text | `model_ids` ★ (comma-joined) | any model used in the run, incl. judges and routed calls |
| `cost` | money | `total_cost_usd` ★ | |
| `cost.input` `cost.output` | money | `input_cost_usd` ★, `output_cost_usd` ★ | |
| `tokens` | number | `total_tokens` ★ | |
| `tokens.in` `tokens.out` `tokens.cached` | number | `prompt_tokens` ★, `completion_tokens` ★, `cache_read_tokens` ★ | |
| `duration` | duration | `total_duration_ms` | |
| `thinking` | duration | `thinking_ms` | |
| `first_token` | duration | `first_token_ms` | |
| `tools` | number | `tool_count` ★ | |
| `tools.failed` | number | `failed_tool_count` ★ | |
| `llm_calls` | number | `llm_call_count` ★ | |
| `report` | text | join `reports.title` | wildcard-friendly |
| `report_id` `run_id` `completion_id` | text | ids | exact |
| `version` | text | `bow_version` | facet |
| `created` | date | `created_at` | |
| bare words | text | `prompt_text` ★ (first 2,000 chars of the user prompt), `error_text` | substring |
| `eval` | boolean | `is_eval_run` | `eval:false` is implied unless the query mentions `eval:` |
| `error` | text | `error_text` ★ (from `error_json.message`) | substring |

### Fields — tool calls (`tool.` prefix)

| Field | Type | Column on `tool_executions` |
|---|---|---|
| `tool` | text | `tool_name` (shorthand for `tool.name`) |
| `tool.action` | text | `tool_action` |
| `tool.status` | enum | `status` |
| `tool.success` | boolean | `success` |
| `tool.attempt` `tool.max_retries` | number | |
| `tool.duration` | duration | `duration_ms` |
| `tool.tokens` | number | `total_tokens` (new column) |
| `tool.cost` | money | `total_cost_usd` (new column) |
| `tool.error` | text | `error_message` |

### Quick filters (built-in saved queries)

| Chip | Query |
|---|---|
| Errors | `status:error` |
| Failed queries | `tool:create_data tool.status:error` |
| Negative feedback | `feedback:negative` |
| Low confidence | `judge.confidence:<3` |
| Low coverage | `judge.instructions:<3` |
| Slow | `duration:>30s` |
| Expensive | `cost:>$0.50` |
| Retried | `tool.attempt:>1` |

A chip is active when the query contains all of its terms as a top-level
conjunction; clicking an active chip removes them.

## Backend

### Denormalised columns on `agent_executions`

`agent_executions` stays the only anchor. Its own columns already cover
status, timings, user, report, version, eval flag and error. Everything the
grammar exposes that is *derived* today — sums over `llm_usage_records`,
counts over `tool_executions`, the judge scores and feedback two hops away on
the parent completion, the prompt text inside a JSON column — becomes a plain
column on `agent_executions` (marked ★ in the field table). Filtering,
sorting and faceting then never aggregate or hop at read time; `users` and
`reports` are joined by primary key for display only, and
`report_data_source_association` only for `agent:` and the security scope.

Why columns and not a side table: the run-end write already updates the row
(status, `completed_at`, timings); adding a dozen values to that UPDATE is
free. Feedback and judge events are low-volume and become one small UPDATE on
the run row. No one-to-one join, no second backfill target, one fewer thing
to keep consistent.

Writers (all idempotent, all in `backend/app/services/diagnosis/rollups.py`):

| Event | Writer | Columns |
|---|---|---|
| Run finished (`AgentExecutionService.finish`) | `rollup_run(run)` | `prompt_text`, `error_text`, `platform`, `tool_count`, `failed_tool_count`, token and cost sums, `llm_call_count`, `primary_model_id`, `primary_provider`, `model_ids` |
| Feedback created/updated (`CompletionFeedbackService`) | `rollup_feedback(completion_id)` | `feedback_direction`, `feedback_message` |
| Judge scores written (wherever `response_score` etc. are set) | `rollup_judge(completion_id)` | `judge_*` |
| Usage record written after run finish (late judge calls) | `rollup_usage(agent_execution_id, record)` | cost/token increments, `cost_is_partial` |

Backfill: `backend/scripts/backfill_agent_execution_rollups.py`, batched by
1,000, resumable, safe to re-run. Historical cost is best-effort (next
section).

### Cost and model attribution per run

`llm_usage_records` has `user_id`, `report_id`, `data_source_id` and a mostly
null `scope_ref_id`; it has no link to the agent execution. Add:

- `agent_execution_id` (indexed, nullable) and `completion_id` (nullable) to
  `llm_usage_records`, populated from the usage-attribution contextvar
  (`app.ai.llm.usage_attribution`) — the contextvar gains both ids, set where
  the run's attribution is set today.
- Rollup at run finish: `SELECT sum(cost), sum(tokens)…, count(*)` over the
  run's records, grouped by `(model_id, provider_type)`; the planner scope's
  model is `primary_model_id`, the full set becomes `model_ids`.
- Historical rows: rows with `report_id` and a timestamp inside a run's
  `[started_at, completed_at]` window attribute to that run in the backfill;
  everything else stays unattributed and the run's `cost_is_partial` flag is
  set, shown as a `~` prefix in the UI.
- Tool-level cost: `tool_executions.total_cost_usd` and `total_tokens` written
  by the tool runner from the records attributed during the call (the
  contextvar carries `tool_execution_id` for the duration of the call).
- Prior art: `TraceModal` already shows a per-report total (sum of
  `llm_usage_records` by `report_id`) and a per-turn figure from
  `usage_events` (the quota ledger, keyed by user completion id, written by
  `UsageLimitContext`, only on instances licensed for usage limits; elsewhere
  the turn falls back to the planner-only `token_usage_json` and shows no
  cost). `UsageLimitContext` is therefore the natural place to set the new
  attribution, since it already knows the run. Once the rollup columns exist,
  the trace endpoint reads per-turn tokens and cost from the run row, so the
  page and the modal agree and the license-gated fallback goes away.

### Modules

```
backend/app/services/diagnosis/
  grammar.py     tokenizer + recursive-descent parser → versioned AST (pure)
  fields.py      FieldSpec registry: name, aliases, type, column, enum values,
                 facetable, entity (run|tool), help text, sugar expansions
  compiler.py    AST → SQLAlchemy clause over agent_executions; tool.* → one
                 correlated EXISTS on tool_executions
  rollups.py     denormalised-column writers + backfill
  service.py     runs / tool_calls / histogram / tools / facets / count
  saved.py       saved queries (per-user v1; built-ins from code)
  api.py         `run_query(db, org, scope, q, range, …)` — the single entry
                 point the HTTP routes AND the future list_agent_runs tool call
```

Compiler rules:

- Every predicate is a bound parameter. `LIKE` values are escaped
  (`\`, `%`, `_`); `*` → `%` after escaping.
- The compiled clause is always AND-ed with `organization_id`, the
  `ConsoleScope` agent subquery (unchanged), the time range, and
  `is_eval_run = false` unless the query mentions `eval:`.
- No dynamic column names, no string-built SQL, no `text()`.
- `EXPLAIN` snapshot tests on Postgres for the five most common shapes
  (plain range, `status:`, `tool:` EXISTS, bare word, `cost:>`) assert the
  expected index is used.

### Endpoints

All under `/console/diagnosis/`, same `manage_settings` gate and `ConsoleScope`.

| Method + path | Purpose | Params |
|---|---|---|
| `GET runs` | page of runs + summary | `q`, `start`, `end`, `tz`, `cursor`, `limit≤100`, `sort` (`created` default; `duration`, `tokens`, `cost`, `tools.failed`) |
| `GET count` | count only, for the typing preview | `q`, `start`, `end`, `tz` → `{count, sampled: bool}` |
| `GET runs/tool_calls` | tool calls for the runs on one page | `run_ids` (≤100) → `{run_id: [ToolCall]}` |
| `GET histogram` | buckets | `q`, `start`, `end`, `tz` → `{bucket, total, matched, matched_errors}` |
| `GET tools` | per-tool strip | `q`, `start`, `end` → `[{tool, calls, errors, p50_ms, cost_usd}]` |
| `GET facets/{field}` | value autocomplete | `q` (minus the term being edited), `start`, `end`, `prefix` → top 20 `{value, count}` |
| `GET fields` | the registry, for syntax help and the TS parser check | → `[{name, aliases, type, values, entity, help}]` |
| `GET saved` / `POST saved` / `DELETE saved/{id}` | saved queries | |

`GET runs` returns:

```json
{
  "items": [{
    "id": "…", "created_at": "…", "status": "error",
    "prompt": "…200 chars…", "user": {"id": "…", "name": "…"},
    "agents": ["SSAS-AW"], "platform": null,
    "model": "gpt-4.1", "provider": "openai",
    "tools": {"total": 5, "failed": 1},
    "duration_ms": 42100, "tokens": 18400, "cost_usd": 0.312, "cost_is_partial": false,
    "judge": {"confidence": 2, "instructions": 4, "context": 3},
    "feedback": "negative",
    "report_id": "…", "completion_id": "…",
    "matched_tool_call_ids": ["…"]
  }],
  "next_cursor": "…",
  "summary": {"matched": 9, "errors": 9, "p50_ms": 12200, "users": 3, "cost_usd": 2.41},
  "total_in_range": 390
}
```

`summary` and `total_in_range` come from the same statement via
`count(*) OVER ()` and conditional aggregates (SQLite ≥ 3.25 and Postgres),
so the table, the summary line and the "of N in range" are one round trip.

Errors: `400 {code: "bad_query", position, message, expected, suggestions}`.
The client parses first, so this is a backstop and the contract for the tool.

Existing endpoints:

- `GET /console/agent_executions/summaries` stays (used by `RecentQueries.vue`,
  `old_agents/[id]/monitoring.vue`, and in-process by `list_agent_executions`).
  Not touched here; the future `list_agent_runs` tool supersedes it.
- `GET /console/diagnosis/metrics`, `…/users`, `…/timeseries`, and
  `GET /console/issues/compact` are removed in the cleanup phase.

### Indexes

`perfidx02_diagnosis_indexes` migration, same existence-checked pattern as
`perfidx01_hot_path_indexes.py` (`ix_ae_org_created` and
`ix_tool_exec_ae_success` already exist). On `agent_executions`, each leading
with `organization_id`, then the predicate, then `created_at` for the range
and cursor:

| Columns after `organization_id` | Serves |
|---|---|
| `created_at, id` | plain range + cursor |
| `status, created_at` | `status:` |
| `user_id, created_at` | `user:` |
| `primary_model_id, created_at` | `model:` facet |
| `primary_provider, created_at` | `provider:` |
| `feedback_direction, created_at` | `feedback:` |
| `total_cost_usd, created_at` | `cost:>`, sort by cost |
| `total_duration_ms, created_at` | `duration:>`, sort |
| `report_id` | scope subquery, `agent:` |

On `tool_executions`: `(agent_execution_id, tool_name)` for the EXISTS,
`(tool_name, status)` for the strip. `llm_usage_records(agent_execution_id)`
for the rollup and the backfill.

Postgres extras behind a dialect check: partial index
`WHERE status = 'error'` on `(organization_id, created_at)`, and
`pg_trgm` GIN on `prompt_text` and `error_text` when the extension is
available (the migration checks `pg_extension` and skips silently).

## Performance budget and how it is met

Targets on a seeded database of 100k runs / 600k tool calls / 1.5M usage
records, Postgres and SQLite, 30-day window, p95: `runs` ≤ 80 ms, `count`
≤ 40 ms, `histogram` ≤ 60 ms, `tools` ≤ 60 ms, `facets` ≤ 40 ms. A script
`backend/scripts/seed_diagnosis_load.py` seeds it and prints timings; both
engines' numbers go in the PR description.

- **Filter on the anchor, join for display.** Every predicate and sort hits
  an indexed column on `agent_executions`; the only other tables in a read
  are `users` and `reports` by primary key and the `tool_executions` EXISTS.
  This is the single biggest lever; everything else is secondary.
- **Bounded by time, always.** The server rejects a request with no
  `start`/`end`. "All time" in the picker sends the org's first run date.
- **Cursor pagination** on `(created_at, id)`; no `OFFSET`.
- **Correlated `EXISTS`** for `tool.*` so runs never duplicate and the planner
  uses `(agent_execution_id, tool_name)`.
- **One statement per panel**, three per commit (`runs`, `histogram`,
  `tools`), sent in parallel. Facets only while a value is being typed.
- **Count preview** uses `GET count`; on Postgres, above 50k rows in range it
  uses the planner's estimate (`EXPLAIN (FORMAT JSON)`) and returns
  `sampled: true`, shown as "≈".
- **Histogram bucket size** chosen server-side from the range (hour ≤ 2 days,
  day ≤ 90 days, week beyond), bucketed in SQL, ≤ 120 buckets.
- **Payload discipline.** Prompt 200 chars, error 500, no `result_json`, no
  `arguments_json`; tool call rows carry `result_summary` only.
- **Short server cache.** An in-process LRU keyed by
  `(org, scope, q, start, end, cursor, sort)` with a 15 s TTL, invalidated
  on rollup writes for that org. Makes back/forward, chip toggling and the
  histogram click feel instant without a second store.
- **Client.** In-flight requests aborted on a new commit; the previous page
  stays on screen while the next loads (no spinner flash, a thin progress bar
  under the query bar); facets debounced 150 ms and cached per
  `(field, prefix, range)`; tool calls for auto-expanded rows fetched with
  the page in one request.

## Frontend

`frontend/pages/monitoring/diagnosis.vue` becomes a thin page composing:

```
frontend/components/diagnosis/
  QueryBar.vue        <input> over a token-coloured <pre> (mirror technique;
                      no contenteditable). "/" focuses, Enter runs, Esc closes
                      suggestions, Tab accepts, ↑↓ navigate. Shows the live
                      count preview and inline error at the right edge.
  QuerySuggest.vue    field / value / operator suggestions from cursor context
  QuickFilters.vue    chips (built-ins + saved), toggle inserts/removes tokens
  TimeRange.vue       presets + custom; emits {start, end, label}
  Histogram.vue       DiagnosisActivityChart with matched/other series
  ToolsStrip.vue      per-tool pills: calls · error% · p50 · cost; click inserts tool:x
  RunsTable.vue       rows + expandable ToolCallRows; opens TraceModal
  ToolCallRows.vue    indented child rows; highlighted when matched
  SyntaxHelp.vue      popover generated from GET fields
frontend/utils/diagnosisQuery.ts
                      the TS parser: tokens for highlighting, AST for chip
                      state, errors with positions + suggestions, cursor
                      context for suggest
frontend/composables/useDiagnosisQuery.ts
                      owns URL state, parse, fetch, abort, debounce, cache
```

Columns in the runs table (from the design): Time · Status · Prompt · User ·
Agent · Model · Tools · Duration · Tokens · Cost · Feedback. Judge scores show
as small numerals on hover of the Feedback cell and as a column when a
`judge.*` term is in the query (column set follows the query; the same rule
adds Provider when `provider:` is used).

Behaviour worth stating:

- Parse on every keystroke (highlight, chip state, error), never fetch on a
  keystroke except the debounced count preview.
- Commit on Enter, chip click, histogram click, tools-strip click, time
  change, and on load from URL.
- Summary line: "9 runs match · of 390 in the last 30 days · 9 errors ·
  p50 12.2s · $2.41 · 3 users".
- Expand state is not in the URL; matched rows auto-expand.
- Empty state teaches the grammar with three example queries as links.
- Dark mode via the same `dark:` tokens the page uses today; the overlay
  colours get dark variants.
- i18n through `$t('monitoring.diagnosis.*')`; `frontend/tests/i18n` must
  pass. Field names and operators are not translated.

Removed from this page: `DateRangePicker` extended mode, `AgentSelector`,
the user `USelectMenu`, the KPI cards, the issue tabs, the day chip.

## The query language as an API (for `list_agent_runs` and friends)

Not built here, but everything below is shaped so the tool is a wrapper:

- `api.run_query(db, org, scope, q, start, end, tz, limit, sort, fields=…)`
  is the single entry point. The HTTP routes call it; the tool calls it
  in-process with `security_data_source_ids` the way `list_agent_executions`
  does today. Scope is enforced inside, never by the caller.
- The tool's input schema is the grammar; its description is generated from
  `fields.py` (`FieldSpec.help`) so the LLM sees the same text as the syntax
  popover. `GET fields` is the machine-readable form.
- Parse errors are structured (`position`, `expected`, `suggestions`) and
  phrased for self-correction: "Unknown field `durations`. Did you mean
  `duration`?" The tool returns them as its error string.
- `fields=` selects response fields so the tool can ask for a compact
  projection; `limit` caps at 100; the tool cannot disable the time bound.
- `explain=true` returns the normalised query string (aliases resolved, sugar
  expanded) so callers can show or store a canonical form.

## Durability: keeping two parsers honest

`backend/tests/fixtures/diagnosis_queries.json` holds golden cases with the
AST version:

```json
{"v": 1, "cases": [
  {"q": "status:error tool:create_data", "ast": {…}, "canonical": "status:error AND tool:create_data"},
  {"q": "duration:>abc", "error": {"position": 9, "message": "Expected a number or duration after >"}},
  {"q": "durations:>3s", "error": {"position": 0, "suggestions": ["duration"]}}
]}
```

`test_diagnosis_grammar.py` (pytest) and `diagnosisQuery.test.ts` (vitest)
load the same file and assert identical ASTs, canonical forms and error
positions. Adding a field means touching `fields.py`, the TS field list and
the fixture; an e2e test compares the TS list against `GET fields`.

## Tests

- **Grammar** (pure): golden fixtures; limits; every error path has a
  position; property test that `parse(canonical(parse(q))) == parse(q)`.
- **Compiler + service** on SQLite via `seed_agent_executions` extended with
  tool executions, feedback, judge scores and usage records: one test per
  field; `tool.*` correlation; wildcard and `LIKE` escaping; `eval:` default;
  scope enforcement (an agent manager cannot see runs outside their agents
  whatever the query); cursor stability; bucket selection; facets respect `q`
  and range; cost rollup and `cost_is_partial`.
- **Rollup writers**: each event updates exactly its columns; writes are
  idempotent; backfill is resumable and matches fresh writes byte for byte.
- **Postgres** run of the same service tests in CI, plus the `EXPLAIN`
  snapshots.
- **Frontend unit**: parser goldens, chip active-state derivation, cursor
  context → suggestion kind, column-set-follows-query.
- **E2E (Playwright, `sandbox-feedback-loop`)**: default range and empty query
  show runs; `status:` shows value suggestions with counts; count preview
  updates while typing; chip → URL → reload restores state;
  `tool.status:error` auto-expands matched rows; row click opens the trace
  modal; cost column shows `~` for partial rows.
- **Load**: `seed_diagnosis_load.py` timings in the PR for both engines.

## Phases

1. **Grammar + fields + AST v1 + goldens** (backend, pure). Small PR,
   reviewable as a spec. Includes the TS parser and its test against the same
   fixture, so the grammar ships in both languages at once.
2. **Rollup columns + attribution**: the ★ columns on `agent_executions`,
   usage-record `agent_execution_id`/`completion_id`, tool cost/token columns,
   writers, backfill script, indexes. Additive; nothing reads them yet.
3. **Compiler + endpoints + `api.run_query`** with service tests, EXPLAIN
   snapshots, load script and timings. Old endpoints untouched.
4. **Frontend**: components, composable, page swap.
5. **Cleanup**: remove the four dead endpoints and service methods, the
   extended `DateRangePicker` mode if unused, old i18n keys; point
   `docs/feedback-loops/diagnosis-filters.md` here.

Each phase is a PR against `main`; 1–3 merge before any UI changes.

## Definition of done

- The decisions table is visible on the page.
- Every field in both field tables works end to end and has a test.
- Golden fixtures pass in both parsers; TS field list matches `GET fields`.
- Load timings under budget on SQLite and Postgres, pasted into the PR.
- Agent-manager scope test passes; the tool entry point enforces it too.
- No request is unbounded in time; no `OFFSET`; no JSON extraction in a
  `WHERE`; no join to `completions`, `completion_feedback` or
  `llm_usage_records` in any read path; `users` and `reports` joined by
  primary key only.
- Backfill run on a production-shaped copy without error; `cost_is_partial`
  rate reported.
- Old endpoints and components removed; `frontend/tests/i18n` green.

## Open questions

1. Saved queries: per-user only in v1, or org-shared with a `shared` flag?
   (Recommendation: per-user in v1; the table has the column.)
2. Should `agent:` accept data source ids as well as names? (Recommendation:
   names in the bar; ids accepted silently for links.)
3. Judge scores are written to the user completion by several code paths
   today. Is there one place to hook `rollup_judge`, or should the backfill
   sweep for changed scores? (Needs a quick audit in phase 2.)
4. Keep `step_titles` (widget names) anywhere on the page? The design drops
   them; the trace modal still shows them.
