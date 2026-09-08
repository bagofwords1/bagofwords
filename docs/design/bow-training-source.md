# Built-in BOW source for training data

Status: implementation plan; no application changes made.

## Outcome and agreed scope

Replace `list_agent_executions` with the ordinary `create_data` workflow targeting a built-in BOW source. Training users can create saved data tables from agent history, reuse those tables, chart them, and refresh them. There is no replacement listing tool and no parallel artifact system.

The source is system-managed: no connection setup, credentials, connector catalog tile, or synthetic agent. It is discoverable only in training mode and only for users authorized to manage agents. Results visibly identify BOW as their source.

Dana can query only data accessible through her current agent `manage` grants. Organization-wide access follows the existing ConsoleScope admin rules. Training-mode access alone never grants access to monitoring data.

First release: runs and tool calls, including existing rollup cost, tokens, feedback, and judge scores. Add standalone usage-record or feedback-history datasets later. Do not expose the application database or arbitrary SQL over internal tables.

## User flow

1. Dana opens training mode. The server resolves her management scope and advertises BOW alongside the other available sources.
2. She asks, “Save a table of failed SQL runs from the last seven days, with user, agent, prompt, cost, and error.”
3. The planner calls `create_data` targeting `bow.runs`. The coder uses the documented BOW client and the diagnosis query grammar.
4. The client fetches the authorized matching dataset, returning a dataframe. Generated code may transform this dataframe as it does other source data.
5. The existing create-data completion handler persists the Query, Step, data, code, parameters, and visualization. BOW provenance and access metadata are persisted with them.
6. Dana uses the result in another query, a chart, a dashboard, or an export. All in-product reads and reuse enforce its BOW access policy.
7. Refresh resolves the executing user's current permissions and relative dates again. The saved data remains a snapshot until refresh; the saved query is the reproducible recipe.

Do not derive a saved table from a small LLM preview or the first page of the Diagnosis UI. Dataset retrieval and preview generation are separate operations.

## Existing integration points

| Responsibility | Current code |
|---|---|
| Source targeting | `backend/app/ai/tools/schemas/create_widget.py` (`TablesBySource`), `schemas/create_data.py` |
| Table resolution and code generation | `backend/app/ai/tools/implementations/create_data.py`, `backend/app/ai/agents/coder/coder.py` |
| Schema and runtime clients | `backend/app/ai/context/builders/schema_context_builder.py`, `context/context_hub.py`, `backend/app/ai/agent_v2.py` |
| Client execution, query capture, timeouts | `backend/app/ai/code_execution/code_execution.py` |
| Artifact persistence | `backend/app/ai/agent_v2.py` create-data completion handling, `backend/app/models/query.py`, `models/step.py` |
| Refresh and parameterized execution | `backend/app/services/query_service.py`, `services/step_service.py` |
| Management scope | `backend/app/core/console_access.py`, `core/permission_resolver.py` |
| Diagnosis predicates and read model | `backend/app/services/diagnosis/{fields,grammar,compiler,service,rollup}.py` |
| Stored-data access and inheritance | `backend/app/services/viewer_data_policy.py`, `services/identity_taint.py`, `backend/app/ai/code_execution/loadables.py` |

Ordinary clients are constructed from persisted DataSource/Connection rows. This source needs an explicit built-in branch in source resolution, rather than a fabricated connection or a UUID inserted into existing foreign-key associations.

## 1. Define source identity and discovery

Use reserved source identifier `builtin:bow`, display name `BOW`, and client key `bow`. Reuse `tables_by_source` with this explicitly supported identifier; update its UUID-only documentation and validation. A normal database source continues to resolve by UUID.

Example create-data targeting:

```json
{
  "title": "Failed SQL runs — last 7 days",
  "user_prompt": "Save a table of failed SQL runs from the last seven days",
  "interpreted_prompt": "Use bow.runs; filter tool:create_data tool.status:error; use a rolling seven-day window; return run_id, report_id, agent names, user, prompt, cost_usd and error.",
  "tables_by_source": [
    {"data_source_id": "builtin:bow", "tables": ["bow.runs"]}
  ],
  "visualization_type": "table"
}
```

Add a shared built-in source resolver, used by discovery, initial execution, and saved execution. It must:

- Advertise BOW only for an authenticated, authorized training request.
- Resolve saved BOW recipes for authorized refreshes without requiring an active chat session. Persisted source references authorize discovery of the recipe, not access to its data.
- Reject forged BOW targeting in an ordinary chat creation request. Switching mode in a prompt is not sufficient.
- Provide a static, versioned schema catalog and client instructions. Values, examples, facets, and sampled rows must be scope-filtered.
- Work in a training report with no connected business source.
- Keep BOW out of normal connection CRUD, agent counts, schema indexing jobs, and report-data-source associations.

Audit source-resolution assumptions in `create_data`, `inspect_data`, schema search, coder context, and captured-query attribution. They must recognize the built-in reference without sending it through UUID lookup or leaking organization-wide schemas/samples. Business-source handling stays compatible.

## 2. Build the scoped query service and client

Proposed modules: `backend/app/services/bow_source_service.py`, `backend/app/schemas/bow_source_schema.py`, and `backend/app/data_sources/clients/bow_client.py`.

Extract the reusable authenticated ConsoleScope resolution from its FastAPI dependency wrapper. HTTP Diagnosis and the BOW service must call the same resolver and report-visibility predicate. Preserve membership checks, admin semantics, and the rule that every agent attached to a report must be managed by a scoped user. Agentless reports remain unavailable to scoped managers.

Scope must be applied before predicates, joins, counts, aggregation, sorting, and pagination. Model-provided agent filters can only narrow it. Tool calls are selected through authorized run IDs. The executor supplies the principal and organization; neither is a query argument generated code can choose.

Expose a typed `execute_query(request)` returning a pandas dataframe, compatible with the executor's existing client wrapper. Example generated code:

```python
def generate_df(ds_clients, excel_files, params):
    return ds_clients["bow"].execute_query({
        "dataset": "runs",
        "query": "tool:create_data tool.status:error",
        "time_range": {"relative": "7d"},
        "columns": ["run_id", "report_id", "agent_names", "user_name",
                    "prompt", "status", "cost_usd", "error"],
        "sort": [{"field": "created_at", "direction": "desc"}]
    })
```

The request schema also supports explicit timezone-aware start/end bounds, grouping, and allowlisted metrics. No raw SQL, DB handles, credentials, arbitrary column expressions, or caller-selected principal. Reject unknown fields and incompatible dataset/metric combinations with actionable errors.

Reuse diagnosis parsing, field semantics, correlated tool predicates, eval defaults, stale status, and rollup values. Extract common query-building primitives as needed; do not scrape nested, display-truncated `run_query` responses into a dataframe or duplicate the SQL compiler. Diagnosis's 200-character prompt preview is not the source dataset contract. Document existing rollup text limits and use source completions only for explicitly supported additional fields.

The executor runs generated code in a worker thread, while diagnosis reads use async SQLAlchemy. Bridge typed requests to a service on its owning event loop with a fresh session per request. Never share the agent's AsyncSession across threads or concurrent calls. Propagate timeout/cancellation through the bridge, and test cancellation does not leave queries running. Keep the bound client surface narrow enough that generated code cannot reach internal session or authorization state.

### Dataset grain and aggregation

| Dataset | Grain | Initial fields |
|---|---|---|
| `bow.runs` | One row per run | Run/report/completion IDs, timestamps, status, prompt/error, user, agent IDs/names, platform, model/provider, cost/partial flag, tokens, duration, feedback, judge scores, tool counts, turn, eval/indexing state |
| `bow.tool_calls` | One row per call | Call/run/report IDs, tool/action/status, attempt, duration, error, safe argument/result preview, created step ID, referenced tables |

Agent IDs/names on run rows are lists. Define `group_by: agent_id` as explicit expansion: multi-agent runs contribute once to each involved agent, so those group totals are not additive. Group on stable IDs and return labels separately. Keep run count and call count distinct; avoid run-cost duplication when joining tool calls. Do not expose encrypted tool-result bodies by default.

Start with count, distinct count, sum, and average, plus hour/day/week time buckets. Apply aggregation over the full authorized match set on the server. Reuse diagnosis date handling with a documented timezone; resolve relative bounds once at the start of each execution, not once per page.

For row queries, issue a bounded server query with deterministic ordering and fetch one extra row to detect overflow. Limits: 30-day default window, 366-day maximum window, 10,000 materialized rows, 1,000 aggregate groups, and the normal executor timeout. If exceeded, return an explicit narrowing/aggregation error rather than silently saving a partial dataset. An explicit user-requested top-N is allowed and recorded in the recipe. Validate these limits against representative data before release.

Exclude the current training report by a trusted server predicate before counts and paging to avoid self-observation. Persist that exclusion in the recipe so refresh semantics are stable. Preserve the partial/unknown cost distinction; do not turn missing data into zero. If eligible matching historical rows are still unindexed, surface indexing incompleteness before presenting or saving a result as complete.

## 3. Persist ordinary artifacts with BOW provenance

Keep the existing create-data artifact path and output shape. Add versioned metadata sufficient to resolve the built-in source on all future runs:

- Query-level source references and schema/query-contract version; no fake Connection FK.
- Step/result-level source lineage, resolved time bounds, requested filters, materialization completeness, and authorization provenance.
- Stable run/report IDs when rows represent executions, enabling authorized trace links and “Open in Diagnosis.” Aggregate results retain their predicate for a diagnosis pivot.

Capture actual BOW client calls and upstream BOW dependencies in trusted execution instrumentation. Do not trust only `tables_by_source`, code-string matching, or model-written metadata: generated code may reuse an upstream table or drop identifying columns.

Extend existing identity/dependency propagation so BOW restrictions survive pandas aggregation, `load_step`, `load_entity`, copied queries, entity publishing, and mixed-source outputs. Dropping run IDs or reducing a dataset to one count does not remove its access requirements.

UI: render the normal data table and existing save/chart/dashboard controls. Add localized “BOW” provenance with the small sidebar logo and an optional Diagnosis pivot; do not recreate the custom execution-list card. Source labels should also work on empty results and refresh errors.

## 4. Enforce saved-data permissions before enabling the source

Query authorization alone is insufficient because Step.data, context summaries, charts, and chat responses copy monitoring data. Extend `viewer_data_policy` and the existing identity model before exposing the new source.

Initial saved-snapshot policy:

- Require ordinary artifact/report access AND current BOW management authorization.
- Record a conservative authorization dependency for each materialization: the managed agent set used to execute it, or an organization-wide marker for admin queries. Narrow this set only when the server can prove an explicit ID restriction; do not infer it merely from which rows happened to be returned.
- A stored snapshot is readable only if the reader's current scope covers that dependency. An organization-wide snapshot requires current organization-wide access. This applies to the creator after revocation too.
- Do not filter an already aggregated snapshot for a narrower viewer. Withhold it and allow an explicit execution under that viewer's own scope, persisted as a viewer result without overwriting the creator's snapshot.
- Record source report dependencies where available and invalidate/recheck when report-agent associations change. Bind cached authorization to current permissions and association revisions; a user-ID-only cache key is insufficient. Use a conservative organization access revision if finer invalidation is unavailable.
- BOW execution always uses the authenticated viewer's scope. Existing `shared_run_identity='creator'` behavior must not lend another user's monitoring permissions. A server-authorized scheduled job uses its designated user's current grants and stops if they are revoked.

Apply the policy before returning cached results, API rows, previews, exports, model context, report summaries, trace material, or streaming payloads. Audit `query_service`, `step_service`, `artifact_service`, `entity_service`, `loadables`, `message_context_builder`, and Step broadcast hooks. A denied dependency must not fall back to shared Step.data.

Restrict the surrounding report/conversation when BOW data enters it, because the assistant's prose can repeat the data. First release uses conservative report-level BOW access dependencies; mixed reports inherit the union of restrictions. Public/token-only report and artifact access, public entity publishing, and unauthenticated render paths must withhold BOW-derived content. Block public publication of an affected report rather than allow partial accidental disclosure.

Internal sharing is allowed only within these access checks. Owner-based schedules may refresh for the owner, but delivery/rendering must authorize every recipient; disable BOW external scheduled delivery until that path is covered. Authenticated export is permitted for an authorized user; an already downloaded file cannot be revoked by the application.

## 5. Replace and remove list_agent_executions

Switch training routing to `create_data` against `builtin:bow`. Remove the old tool in the same release that enables this source:

- Delete `backend/app/ai/tools/implementations/list_agent_executions.py` and `backend/app/ai/tools/schemas/list_agent_executions.py`; confirm auto-discovery no longer advertises it.
- Rewrite training instructions and examples in `backend/app/ai/agents/planner/prompt_builder_v3.py`. Agent-performance requests should directly target BOW without asking users to connect a database.
- Update `backend/app/ai/skills/library/{audit-instructions,usage-review,create-evals,train-agent}.md` to query BOW and reuse saved Query/Step results. Preserve these workflows' need for prompt, feedback, tool evidence, report/completion IDs, and supported answer context.
- Replace both old result-context branches in `backend/app/ai/context/builders/message_context_builder.py` with the normal saved-artifact context path for new results.
- Delete `frontend/components/tools/ListAgentExecutionsTool.vue` and remove its imports/cases from report pages, the shared report page, TraceModal, and block grouping. New calls render through the existing create-data components.
- Review the old tool-name exclusions in `console_service.py`: use source/execution provenance where the intent is to exclude self-analysis, rather than exclude all `create_data` calls.
- Update `backend/tests/evals/suites/sanity_training.yaml` and any tool-list/schema snapshots. Assert the old tool is unavailable and generated training calls target BOW.

Do not delete historical tool execution records. Keep their names as audit history; render their records through a generic authorized historical-tool renderer, with no executable compatibility alias and no special old listing UI. Suppress unsafe historical result bodies on unauthorized/public paths. Historical names may remain in fixtures and compatibility recognition; no live prompt should instruct a new call to the removed tool.

## 6. Implementation sequence

1. **Contract and authorization foundation:** dataset/request schemas, built-in source identity, shared scope resolver, saved-result access/provenance migrations, and negative authorization tests.
2. **Query service and adapter:** scoped row queries, metrics, completeness limits, execution bridge, cancellation, and parity with diagnosis. Keep discovery disabled while building.
3. **Create-data integration:** schema discovery, source targeting, coder instructions, captured query provenance, normal persistence and table rendering. Handle BOW-only reports.
4. **Artifact lifecycle:** all rerun paths, parameters, viewer caches, saved-result reads, lineage propagation, report-level protection, internal sharing, and authorized exports. Prove revoked access cannot reuse caches or old snapshots.
5. **Cutover:** update training prompts/skills/evals, remove `list_agent_executions`, and enable training-only source discovery together. No public phase exposing both tools.
6. **Live verification and documentation:** seeded multi-user reproduce/save/reuse/refresh loop, before/after UI evidence, performance checks on SQLite and PostgreSQL, then product docs and release notes when shipped.

Schema migrations must be additive and reversible. This builds on the diagnosis rollup migration: finish the diagnosis frontend/API alignment and validate startup backfill before enabling BOW. PR #1099 is unreleased: the BOW provenance columns are folded into its existing `diagrollup01` migration; no additional revision is introduced. A rollout feature gate may temporarily disable the new source; it must not bypass access policy or resurrect the removed tool. Operational rollback is a coherent prior application release with compatible schema.

## 7. Verification and acceptance criteria

Read `backend/tests/AGENTS.md` before implementation tests. Follow sandbox-feedback-loop for the live fixture; ui-evidence and localization for UI changes; docs-update and release-notes after shipping.

Seed an admin, Dana managing Chocolate, Omer managing a different agent, a member with ordinary agent access only, another organization, and reports using both managed and unmanaged agents. Include runs with retries, failed tool calls, unknown/partial cost, feedback, judge scores, eval runs, and unindexed history.

Required automated and live checks:

- Training discovery appears for authorized users only; ordinary mode does not advertise it. Direct spoofed source targeting, organization IDs, agent filters, and principal inputs cannot expand permissions.
- Dana's rows, calls, facets, aggregates, and exported tables contain only authorized data. A mixed-agent report is excluded unless she manages every associated agent. Admin behavior matches ConsoleScope.
- Equivalent diagnosis filters and BOW queries return the same matching run IDs. Verify same-call tool correlation, date/timezone handling, eval defaults, partial cost, and explicit agent grouping semantics.
- More than the normal UI preview cap materializes correctly; aggregate totals cover all matches. Top-N is explicit, limits are visible, incomplete indexing is not represented as complete, and relative bounds remain fixed throughout each execution.
- A real training prompt creates a Query/Step with stored data and code, visible after reload. Save/chart/dashboard/export and follow-up `load_step`/`load_entity` use ordinary artifacts. Refresh reflects newly inserted runs and advances relative dates.
- New execution, saved-code rerun, parameterized viewer execution, and supported scheduled refresh all reconstruct the BOW client with the correct current principal.
- Revoking Dana's management role blocks reads and refreshes, including cached data, model context, downloaded-in-app previews, and streaming. Changing source report-agent associations invalidates stale access. Omer cannot see Dana's broader saved snapshot; an authorized personal rerun cannot overwrite it.
- Restricted lineage survives aggregation, removed IDs, copying, entity publication, dashboard rendering, and mixed-source joins. Public/share-token paths do not expose rows, chart values, prompts, generated summaries, or previews.
- No new planner call can select `list_agent_executions`. Existing audit history remains readable when authorized. Training skill/eval scenarios now produce reusable BOW data artifacts.
- The adapter does not block the event loop or share sessions across threads. Cancellation and errors release resources. Existing source clients, create-data flows, and viewer-data-policy tests remain green on both database engines.

Done means a permitted training user can create, save, reuse, and refresh BOW tables through `create_data`; the management-scope boundary holds across their lifecycle; and the previous execution-list tool is removed.
