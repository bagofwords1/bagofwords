# Plan: Diagnosis explorer — one query bar over agent runs and their tool calls

Design canvas: https://claude.ai/code/artifact/6a9f2648-ba59-4306-8a35-f91d2b03bc1b
(the "Agent runs" board is the target; the "Tool calls" board is superseded by
expandable rows + the tools strip decided below).

## Mission

Replace `/monitoring/diagnosis` with a log-explorer page: one query language
over `agent_executions` and their `tool_executions`, one histogram, one table,
everything derived from the same parsed query. Clean and minimal on screen,
strict and fast underneath.

Principles, in priority order:

1. **One source of truth.** The query string in the URL is the whole page
   state. Chart, table, summary line, tools strip and facets are all
   projections of it. No second filter system, ever.
2. **Durable.** The grammar is a spec with golden tests shared by the Python
   and TypeScript parsers. Fields are an explicit allowlist, so the API surface
   cannot drift by accident. Saved queries are plain strings.
3. **Fast.** Bounded time range by default, index-backed predicates, one
   round-trip per panel, cursor pagination, no unbounded scans, no N+1.

## Non-goals (v1)

- A globally sorted/paged list of tool calls across runs (covered by the tools
  strip; can be added later as a drill-down from it).
- Aggregations beyond the histogram and the per-tool strip (no group-by UI).
- Full-text search engine (Postgres FTS / trigram). Plain `ILIKE` bounded by
  the time range is enough for v1; see Performance for the upgrade path.
- Touching `/monitoring` (Explore) or `/monitoring/cost`.

## Decisions carried over from the design review

| Decision | Choice |
|---|---|
| Entity model | Agent runs are the rows. Each row expands to its tool calls. No runs/tool-calls toggle. |
| Tool-level questions | `tool.*` fields in the query match runs containing such a call; matching calls are highlighted and auto-expanded. A per-tool strip (calls, error rate, p50) above the table answers aggregate questions. |
| Old issue tabs | Become quick-filter chips that insert tokens into the query. They light up when the query contains their tokens. |
| KPI cards | Removed. One summary line above the chart, computed from the current query. |
| Agent / user dropdowns | Removed from this page. `agent:` and `user:` fields with facet autocomplete replace them. Security scope (agent managers) is unchanged and server-side. |
| Time | Picker with presets + custom; default **Last 30 days**, never all-time by default. Clicking a histogram bar inserts a `created:` token. |
| URL | `?q=…&range=30d&cursor=…` is the page state; links are shareable. |
| Row click | Opens the existing `TraceModal` (`report_id` + `completion_id`), unchanged. |

## Query language

KQL-shaped. Whitespace-separated terms, implicit `AND`.

```
query     := orExpr
orExpr    := andExpr ( "OR" andExpr )*
andExpr   := notExpr ( ("AND")? notExpr )*
notExpr   := ("NOT" | "-")? primary
primary   := "(" orExpr ")" | term
term      := field ":" value          -- field predicate
           | field ":" "(" value ("OR" value)* ")"
           | phrase | word            -- bare text (full-text)
value     := comparison | range | phrase | word | wildcard
comparison:= (">" | ">=" | "<" | "<=") scalar
range     := scalar ".." scalar
scalar    := number | duration | date | word
duration  := number ("ms" | "s" | "m" | "h")
date      := YYYY-MM-DD | YYYY-MM | "today" | "yesterday" | "-7d"
phrase    := '"' … '"'
wildcard  := word containing "*"
```

Semantics:

- Bare words and phrases search `prompt`, `error` and tool `error` text
  (case-insensitive substring). A phrase is one substring.
- `field:value` on an enum/text field is equality (case-insensitive for text),
  `field:*sql*` is a wildcard, `field:(a OR b)` is any-of.
- Comparisons and ranges are valid on number, duration and date fields.
  Durations accept bare milliseconds too (`duration:>30000`).
- `has:<field>` means the field is non-null.
- A date without a time means the whole day (in the caller's timezone, sent as
  `tz` param); `created:2025-08` means the whole month.
- **`tool.*` clauses correlate to one tool call.** All `tool.*` terms in the
  query must match the same `tool_executions` row (one correlated `EXISTS`).
  This is what people mean by "runs with a failed create_data call". Document
  it in the syntax help; do not try to be cleverer in v1.
- Unknown field → error with position. Unknown enum value → error listing the
  values. Both are caught client-side before a request is sent.
- Limits: 1,000 characters, 40 terms, nesting depth 4. Enforced in both parsers.

### Fields — agent runs

| Field | Type | Source | Notes |
|---|---|---|---|
| `status` | enum | `agent_executions.status` | `completed` `error` `in_progress` |
| `user` | text | `users.name` / `users.email` | facet |
| `agent` | text | data source name via `report_data_source_association` | facet; multi-agent runs match any |
| `platform` | enum | `completions.external_platform` (user completion) | `web` is `NULL` |
| `feedback` | enum | `completion_feedback.direction` | `positive` `negative` `none` |
| `confidence` | number 1–5 | `completions.response_score` (parent user completion) | judge |
| `coverage` | number 1–5 | `completions.instructions_effectiveness` | judge |
| `duration` | duration | `total_duration_ms` | |
| `thinking` | duration | `thinking_ms` | |
| `first_token` | duration | `first_token_ms` | |
| `tokens` `tokens.in` `tokens.out` | number | `token_usage_json` | see "Denormalise" below |
| `report` | text | `reports.title` | wildcard-friendly |
| `report_id` `run_id` | text | ids | exact |
| `created` | date | `agent_executions.created_at` | |
| `eval` | boolean | `is_eval_run` | default `false` is applied unless the query mentions `eval:` |
| `error` | text | `error_json.message` | substring |

### Fields — tool calls (`tool.` prefix)

| Field | Type | Source |
|---|---|---|
| `tool` | text | `tool_executions.tool_name` (shorthand for `tool.name`) |
| `tool.action` | text | `tool_action` |
| `tool.status` | enum | `status` |
| `tool.success` | boolean | `success` |
| `tool.attempt` `tool.max_retries` | number | |
| `tool.duration` | duration | `duration_ms` |
| `tool.tokens` | number | `token_usage_json` total |
| `tool.error` | text | `error_message` |

### Quick filters (built-in saved queries)

| Chip | Query |
|---|---|
| Errors | `status:error` |
| Failed queries | `tool:create_data tool.status:error` |
| Negative feedback | `feedback:negative` |
| Low confidence | `confidence:<3` |
| Low coverage | `coverage:<3` |
| Slow | `duration:>30s` |
| Retried | `tool.attempt:>1` |

Chips are "active" when the query contains all of the chip's terms as a
conjunction at the top level; clicking an active chip removes them.

## Backend

New package `backend/app/services/diagnosis/`:

```
diagnosis/
  grammar.py      tokenizer + recursive-descent parser → AST (pure, no SQLAlchemy)
  fields.py       FieldSpec registry: name, type, column/expr factory, enum values,
                  facetable, entity (run|tool). The allowlist.
  compiler.py     AST → SQLAlchemy BooleanClause. tool.* → one correlated EXISTS.
  service.py      list / histogram / tools strip / facets / tool calls for a page
  saved.py        saved query CRUD (v1: per-user; built-ins from code)
```

Rules for the compiler:

- Every predicate is a bound parameter. `LIKE` values are escaped
  (`\`, `%`, `_`). Wildcards translate `*` → `%` only after escaping.
- The compiled clause is always AND-ed with: `organization_id = :org`,
  the `ConsoleScope` report subquery (unchanged from today), the time range,
  and `is_eval_run = false` unless the query mentions `eval:`.
- JSON fields (`token_usage_json`) are read through a small dialect helper
  (`json_extract` on SQLite, `->>` on Postgres). Cast once, in `fields.py`.
- No dynamic column names, no string-built SQL, no `text()`.

Endpoints (all under `/console/diagnosis/`, same `manage_settings` gate and
`ConsoleScope` as today):

| Method + path | Purpose | Params |
|---|---|---|
| `GET runs` | page of runs | `q`, `start`, `end`, `tz`, `cursor`, `limit≤100`, `sort` (`created` default, `duration`, `tokens`) |
| `GET runs/tool_calls` | tool calls for the runs on one page | `run_ids` (≤100) → `{run_id: [ToolCall]}` |
| `GET histogram` | buckets by status | `q`, `start`, `end`, `tz` → `{bucket, total, matched}` |
| `GET tools` | per-tool strip | `q`, `start`, `end` → `[{tool, calls, errors, p50_ms}]` |
| `GET facets/{field}` | value autocomplete | `q` (without the term being edited), `start`, `end`, `prefix` → `[{value, count}]` top 20 |
| `GET saved` / `POST saved` / `DELETE saved/{id}` | saved queries | |

Response shape for a run row (lean; nothing the table does not show):

```json
{
  "id": "…", "created_at": "…", "status": "error",
  "prompt": "first 200 chars", "user": {"id": "…", "name": "…"},
  "agents": ["SSAS-AW"], "platform": null,
  "tools": {"total": 5, "failed": 1},
  "duration_ms": 42100, "tokens": 18400,
  "feedback": "negative", "confidence": 2, "coverage": 4,
  "report_id": "…", "completion_id": "…",
  "matched_tool_call_ids": ["…"]
}
```

`GET runs` returns `{items, next_cursor, total, summary: {matched, errors, p50_ms, users}}`.
`total` is computed with `count(*) OVER ()` in the same statement (window
functions are available in SQLite ≥ 3.25 and Postgres), so list + summary is
one round trip.

Errors: a query that fails to parse returns `400 {code: "bad_query",
position, message, expected}`. The client parses first, so this path is only a
backstop.

Existing endpoints:

- `GET /console/agent_executions/summaries` stays (used by
  `RecentQueries.vue`, `old_agents/[id]/monitoring.vue`, and in-process by the
  `list_agent_executions` tool). Not touched in this work.
- `GET /console/diagnosis/metrics`, `GET /console/diagnosis/users`,
  `GET /console/diagnosis/timeseries`, `GET /console/issues/compact` are
  removed in the cleanup phase once nothing calls them.

### Denormalise what the query touches

`token_usage_json` is a JSON blob and `tokens` is a filterable, sortable field.
Add nullable integer columns `total_tokens` to `agent_executions` and
`tool_executions`, written at the same time as the JSON (one line in
`AgentExecutionService` / the tool runner), with a one-off backfill migration.
Filtering on JSON extraction in a `WHERE` is the single easiest way to make this
page slow, and the column is cheap. Everything else the grammar exposes is
already a real column.

### Indexes

Add through a `perfidx02_diagnosis_indexes` migration using the same
existence-checked pattern as `perfidx01_hot_path_indexes.py`
(`ix_ae_org_created` and `ix_tool_exec_ae_success` already exist):

| Index | Columns | Serves |
|---|---|---|
| `ix_ae_org_status_created` | `agent_executions(organization_id, status, created_at)` | `status:` + range |
| `ix_ae_org_user_created` | `agent_executions(organization_id, user_id, created_at)` | `user:` |
| `ix_ae_report` | `agent_executions(report_id)` | scope subquery, `agent:` |
| `ix_tool_exec_ae_tool` | `tool_executions(agent_execution_id, tool_name)` | correlated EXISTS |
| `ix_tool_exec_tool_status` | `tool_executions(tool_name, status)` | tools strip |
| `ix_feedback_completion` | `completion_feedback(completion_id)` | `feedback:` (verify not present) |

## Performance budget and how it is met

Targets on a seeded database of 100k runs / 600k tool calls, Postgres and
SQLite, 30-day window: `runs` ≤ 150 ms p95, `histogram` ≤ 100 ms, `tools` ≤
100 ms, `facets` ≤ 80 ms. A script `backend/scripts/seed_diagnosis_load.py`
seeds it and prints timings; run before merge on both engines.

- **Bounded by time, always.** The server rejects a request with no
  `start`/`end` (the client always sends the picker's range). "All time" in the
  picker sends the org's first run date, so it is still a range.
- **Cursor pagination** on `(created_at, id)`; no `OFFSET`.
- **Correlated `EXISTS`** for `tool.*` rather than a join, so runs never
  duplicate and the planner can use `ix_tool_exec_ae_tool`.
- **One statement per panel**, three panels per keystroke-commit (`runs`,
  `histogram`, `tools`). Facets fire only while a value is being typed.
- **Tool calls for a page in one query** (`run_ids IN (…)`), requested only
  for expanded rows; rows with `matched_tool_call_ids` are expanded on load,
  so the client requests those ids together with the page.
- **Histogram bucket size** chosen server-side from the range (hour ≤ 2 days,
  day ≤ 90 days, week beyond), bucketed in SQL, ≤ 120 buckets returned.
- **Payload discipline.** Prompt truncated to 200 chars, error to 500, no
  `result_json`, no `arguments_json` in list responses. Tool call rows carry
  `result_summary` only.
- **Client.** In-flight requests are aborted on a new commit
  (`AbortController`); facet lookups are debounced 150 ms and cached per
  `(field, prefix, range)` for the session; the last successful page is kept on
  screen while a new one loads (no spinner flash).
- **Full-text upgrade path.** If bare-word search on prompts is too slow at
  scale on Postgres, add a generated `prompt_text` column on `completions` with
  a `pg_trgm` GIN index behind a dialect check. Not in v1; the 30-day bound
  makes `ILIKE` acceptable.

## Frontend

`frontend/pages/monitoring/diagnosis.vue` becomes a thin page composing:

```
frontend/components/diagnosis/
  QueryBar.vue        input + syntax-highlight overlay (mirror technique: a
                      transparent <input> over a <pre> with coloured tokens;
                      no contenteditable), keyboard: "/" focus, Enter run,
                      Esc clear suggestions, Tab accept
  QuerySuggest.vue    field / value / operator suggestions from the cursor context
  QuickFilters.vue    chips (built-ins + saved), toggle inserts/removes tokens
  TimeRange.vue       presets + custom; emits {start, end, label}
  Histogram.vue       reuse DiagnosisActivityChart with matched/other series
  ToolsStrip.vue      per-tool pills: calls · error% · p50; click inserts tool:x
  RunsTable.vue       rows + expandable ToolCallRows; opens TraceModal on click
  ToolCallRows.vue    indented child rows; highlighted when matched
frontend/utils/diagnosisQuery.ts
                      the TS parser: tokens for highlighting, AST for chip
                      state, errors with positions, cursor context for suggest
frontend/composables/useDiagnosisQuery.ts
                      owns URL state, parse, fetch, abort, debounce
```

Behaviour worth stating:

- Query runs on Enter, on chip click, on histogram click, on time change, and
  on load from URL. Not on every keystroke.
- The tools strip and histogram reflect the *committed* query; the summary
  line reads "9 runs match · of 390 in the last 30 days · 9 errors · p50 12.2s · 3 users".
- Expand state is not in the URL; matched rows auto-expand.
- Empty state teaches the grammar with three example queries as links.
- Syntax help is a popover from a `?` icon in the bar, generated from the
  field registry served by `GET /console/diagnosis/fields` so the docs cannot
  drift from the allowlist.
- Dark mode uses the same tokens the current page uses (`dark:` variants of
  gray/blue); the overlay colours get dark variants.
- i18n: all strings through `$t('monitoring.diagnosis.*')`; the existing
  `frontend/tests/i18n` checks must pass. Field names and operators are not
  translated.

Removed from this page: `DateRangePicker` extended mode, `AgentSelector`,
the user `USelectMenu`, the KPI cards, the issue tabs, the day chip.

## Durability: keeping two parsers honest

`backend/tests/fixtures/diagnosis_queries.json` holds golden cases:

```json
[{"q": "status:error tool:create_data", "ast": {...}, "ok": true},
 {"q": "duration:>abc", "ok": false, "position": 9, "message": "Expected a number or duration"}]
```

Both `test_diagnosis_grammar.py` (pytest) and `diagnosisQuery.test.ts` (vitest)
load the same file and assert identical ASTs and identical error positions.
Adding a field means adding it to `fields.py`, the TS field list, and the
fixture; a test fails if the three disagree (the TS list is checked against
`GET /console/diagnosis/fields` in an e2e test).

## Tests

- **Grammar** (pure): golden fixtures; limits; every error path has a position.
- **Compiler + service** (SQLite via existing `seed_agent_executions`, extended
  with tool executions, feedback and judge scores): one test per field;
  `tool.*` correlation (two `tool.*` terms must hit the same call); wildcard
  and `LIKE` escaping; `eval:` default; scope enforcement (an agent manager
  cannot see runs outside their agents no matter the query); cursor
  pagination stability; histogram bucket selection; facets respect `q` and
  range.
- **Postgres** run of the same service tests in CI (the JSON helper and
  window functions are the dialect-sensitive parts).
- **Frontend unit**: parser goldens, chip active-state derivation, cursor
  context → suggestion kind.
- **E2E (Playwright, `sandbox-feedback-loop`)**: load page → default range and
  empty query show runs; type `status:` → value suggestions with counts; run
  chip → URL updates → reload restores state; `tool.status:error` auto-expands
  matched rows; row click opens the trace modal.
- **Load**: `seed_diagnosis_load.py` timings printed in the PR description for
  both engines.

## Phases

1. **Grammar + fields + compiler + goldens** (backend, pure). Small PR, no
   endpoints, no UI. Reviewable as a spec.
2. **Endpoints + denormalised token columns + indexes + service tests**
   (backend, additive). Old endpoints untouched. Load script and timings.
3. **Frontend**: TS parser + goldens, then components, then the page swap.
   Ships behind nothing; the page is replaced in this PR.
4. **Cleanup**: remove the four dead endpoints and their service methods,
   the extended `DateRangePicker` mode if nothing else uses it, and the old
   i18n keys. Update `docs/feedback-loops/diagnosis-filters.md` to point here.

Each phase is a separate PR against `main`; phases 1–2 can merge before any UI
exists.

## Definition of done

- The four decisions in the table above are visible on the page.
- Every field in the two field tables works end to end and has a test.
- Golden fixtures pass in both parsers; `GET /console/diagnosis/fields` matches
  the TS field list.
- Load timings under budget on SQLite and Postgres, pasted into the PR.
- Agent-manager scope test passes.
- No request from the page is unbounded in time; no `OFFSET`; no JSON
  extraction in a `WHERE`.
- Old endpoints and components removed; `frontend/tests/i18n` green.

## Open questions

1. Saved queries: per-user only in v1, or org-shared with a `shared` flag from
   the start? (Recommendation: per-user in v1; the table has the column.)
2. Should `agent:` accept data source ids as well as names? (Recommendation:
   names only in the bar, ids accepted silently for links.)
3. Keep `step_titles` (widget names) anywhere on the page? The design drops
   them; the trace modal still shows them.
