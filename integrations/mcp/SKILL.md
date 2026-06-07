---
name: bagofwords
description: Query, visualize, and analyze data in a Bag of Words (BOW) workspace via the bagofwords MCP tools. Use for creating reports, running tracked queries, and building dashboards.
---

# Bag of Words (BOW) Analytics

BOW is a data-analytics workspace connected to the organization's databases and
files. You drive it through the `bagofwords` MCP tools. This skill defines how to
use those tools well and the analytical standards to hold yourself to.

> If the `bagofwords` MCP tools are not available in this conversation, the BOW
> connector isn't connected. Tell the user to connect the Bag of Words connector
> first, then retry — don't attempt to answer data questions without it.

## Core workflow

Follow this sequence. Don't skip the research steps.

1. **Start a session — `create_report` (once).** At the start of an analysis,
   call `create_report` with a short descriptive `title` derived from the user's
   ask. Keep the returned `report_id` and pass it to every later call. Do NOT
   create a new report per question in the same analysis — reuse the same one.
2. **Discover the data — `get_context`.** Before querying, call `get_context`
   to see data sources/agents, tables, columns, and metadata resources. Optionally
   filter with regex. Never assume table or column names — confirm them here.
3. **Validate assumptions — `inspect_data` (optional, sparing).** Use only for a
   quick peek (it returns ~3 sample rows): check nulls, distinct values, join
   keys, date formats. It is NOT analysis and is not saved. Max 2–3 probes.
4. **Produce the answer — `create_data`.** This runs a tracked, reproducible
   query + visualization that is saved and shareable. Use it for any real
   result the user should keep. Never present `inspect_data` output as the answer.
5. **Compose dashboards — `create_artifact` / `edit_artifact`.** Build a
   dashboard or slides from visualizations you already created.

## Tools

- `create_report(title)` — start an analysis; returns `report_id`. Call once, reuse.
- `get_context(report_id, [regex filters])` — list data sources, tables, columns,
  and metadata. Use before writing any query and to look up business definitions.
- `inspect_data(report_id, ...)` — quick 3-row preview for validation only.
- `create_data(report_id, ...)` — tracked query + chart/table. The real answer.
- `create_artifact(report_id, mode)` — dashboard (`mode: "page"`) or presentation
  (`mode: "slides"`) from existing visualizations (auto-uses up to 10).
- `edit_artifact(report_id, artifact_id, ...)` — targeted change to an existing
  artifact. Needs `artifact_id`. Prefer this over rebuilding for small edits.
- `list_instructions(...)` — read the org's business rules / metric definitions.
- `create_instruction` / `delete_instruction` — only if available to this user
  (permission-gated); don't rely on them being present.

## Ask before you guess (clarify discipline)

There is no clarify tool here — when something is ambiguous, **ask the user in
plain text** before calling a data tool. Ask when:

- The user names a business term, metric, or KPI that isn't defined in the org
  instructions and can't be mapped unambiguously to one column/table
  (e.g. "active users", "churn", "engagement", "high-value customer").
- They ask for a definition or "what counts as X".
- Scope is unclear: time window, entity, threshold, granularity, or which of
  several plausible interpretations applies.
- The data covers only part of the ask and you'd have to guess to fill the gap.

Never invent a definition. Offer 2–4 concrete candidate interpretations grounded
in the schema/instructions, and end with "or specify your own." One clarifying
question beats building the wrong thing.

## Schema & semantic-layer discipline

- Never hallucinate table or column names — confirm via `get_context`.
- Tables flagged as having instructions carry business rules; read them
  (`get_context` / `list_instructions`) before writing queries against them.
- For a business term, check org instructions first; use that definition if found.
- Tables under different connections are **separate databases** — you cannot join
  across connections.

## Building dashboards

- **Cold start (no existing visualizations):** build ONE wide master table
  covering the metrics + dimensions the dashboard needs, then `create_artifact`.
  Don't fire off 3–4 narrow queries (separate KPI / trend / top-N) — the artifact
  derives those from one wide result.
- **Warm start (visualizations already exist):** demonstratives like "this data",
  "the above", or "great, build a dashboard" mean REUSE what's there. Go straight
  to `create_artifact` with the existing visualizations — don't re-run `create_data`
  unless a specific column the user named is missing from every existing viz.
- **Small change to an existing dashboard:** use `edit_artifact` with its
  `artifact_id`. Only `create_data` first if the change needs new data.
- **When unsure** which visualizations to use or the ask is generic ("a nice
  overview"), ask the user with 2–3 concrete options rather than guessing.

## Analytical standards

- Shape output to the question: "how many" → a scalar/COUNT; "top N" → N rows;
  a list → rows with the fields the user cares about. Include identity columns
  (keys) on row results so follow-up drill-downs don't need a re-query.
- Ground every claim in the data; cite the table/column/source. Distinguish
  "the data shows X" from "I infer X".
- State confidence on inferences; acknowledge data limitations; flag anomalies
  (unexpected zeros, sudden changes, outliers).
- Don't fabricate data, secrets, or credentials. If something required is
  missing, ask the user.
- In your final message, summarize findings in plain language — don't dump raw
  rows or surface internal IDs (report/visualization/artifact ids).
