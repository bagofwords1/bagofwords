# Feedback Loop — "the report does not have the right context, even though the prompt box has it"

The prompt box (`PromptBoxV2` → `DataSourceSelector`) shows a data source
selected — e.g. *Music Store* / an agent — but when a completion runs the agent
answers that it is connected to **no** data source ("כרגע אני לא מחובר לאף מקור
נתונים" / "I have no access to any data source, files or connectors"). In the
second report the selector is on **Auto** (all sources selected) yet the agent
only "sees" a subset. The context the model receives is a **strict subset** of
what the prompt box shows.

This loop validates the claim that the prompt box and the running agent decide
"is this data source live?" with **different, disagreeing rules**, so a source
can be shown + selected + persisted on the report while contributing nothing to
the agent's context.

## Root cause (validated)

The two sides of the app apply different liveness gates to a report's attached
data sources:

* **Prompt box / report side — data-source lifecycle only.**
  `GET /reports/{id}` serializes `report.data_sources` through
  `DataSourceService.filter_live_data_sources`
  (`backend/app/services/report_service.py:348`, `:1694`), which keeps a source
  when `DataSourceService.is_execution_live` is true. `is_execution_live`
  (`backend/app/services/data_source_service.py:1943-1956`) checks only
  `DataSource.is_active` and `publish_status != "disabled"` — **it never looks
  at connection health or per-user access.** `PromptBoxV2.hydrateReportDataSources`
  (`frontend/components/prompt/PromptBoxV2.vue:982-1008`) then shows exactly that
  set. So the prompt box keeps showing the source.

* **Agent side — must also have a live client.**
  When a completion runs, `CompletionService` builds clients per source and
  hands them to the agent (`backend/app/services/completion_service.py:348-386`).
  `AgentV2.__init__` then **drops any report data source that produced no
  client** via the `_has_client` guard
  (`backend/app/ai/agent_v2.py:299-320`, especially `:313-320`). Clients come
  from `DataSourceService.construct_clients`, which builds them **only from
  *active* connections**:
  `active_connections = [c for c in ds.connections if c.is_active]`
  (`backend/app/services/data_source_service.py:2132-2135`), and additionally
  raises `403` for a source the run-user can't access
  (`:2109-2117`, caught + skipped at `completion_service.py:359-363`).

`Connection.is_active` is a **system-managed health flag**, auto-toggled by
connection-test results (`backend/app/models/connection.py:23`, and the comment
at `data_source.py:24-27`). A failed reachability test flips it to `False`.
When that happens — or when the run-user isn't a member of the source — the
source yields **no client**, so `_has_client` removes it from
`self.data_sources` *after* it already passed the `is_execution_live` gate that
the prompt box/report rely on. `SchemaContextBuilder` independently drops the
same source's tables for the same reason
(`backend/app/ai/context/builders/schema_context_builder.py:172`, `:214`).

Net effect: **the prompt box's selected/persisted set and the set the agent
actually receives are computed by different filters.** Any source that is
`is_execution_live` but has no queryable client (unhealthy connection, or a
source the run-user can't access) stays visible and selected in `PromptBoxV2`
while silently vanishing from the agent context — which is exactly the reported
"was connected, now isn't / prompt box has it, report doesn't" symptom.

> Not the cause: `is_execution_live` keeps `training`/`development`
> `reliability_status` and `draft` `publish_status` sources (only `disabled` /
> inactive are dropped), so the "Training" badge in the screenshots is not what
> removes them — the drop is the missing-client gate above.

## Loop A — deterministic reproduction (no external services)

`backend/tests/unit/test_report_context_datasource_dropped.py` seeds an org +
one published, active data source wired to a single connection, and toggles the
**connection health flag** (`Connection.is_active`). No LLM, no live database —
the drop happens during context assembly, before the model is ever called.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"   # required by bow-config.dev.yaml
uv run python -m pytest tests/unit/test_report_context_datasource_dropped.py -p no:warnings -v
```

Observed (both pass — the second *is* the bug):

```
test_healthy_connection_source_reaches_agent_context PASSED
test_unhealthy_connection_source_kept_by_selector_but_dropped_by_agent PASSED
```

* `test_healthy_connection_source_reaches_agent_context` — baseline: healthy
  connection → `is_execution_live` true **and** `construct_clients` yields a
  client → the agent's `_has_client` filter keeps the source.
* `test_unhealthy_connection_source_kept_by_selector_but_dropped_by_agent` —
  flip `Connection.is_active = False`:
  * `is_execution_live(ds)` is **still True**, and
    `filter_live_data_sources([ds], …)` **still returns the source** → the
    prompt box/report keep showing it;
  * `construct_clients(...)` now returns `{}` → the agent's `_has_client`
    guard removes the source → the agent context is empty.

  The single assertion that captures the mismatch:

  ```python
  assert DataSourceService.is_execution_live(ds) is True          # prompt box keeps it
  assert clients == {}                                            # agent gets no client
  assert _agent_has_client_filter([ds], clients) == []           # agent drops it
  ```

## Loop B — live confirmation (optional)

Not required to prove the root cause: the source is dropped during context
assembly (`AgentV2.__init__` / `ContextHub` / `SchemaContextBuilder`) **before**
any LLM call, so the model never sees it regardless of provider. A live run
would only reproduce the same drop plus the model's "no data source connected"
phrasing. To stage it, boot the stack (`tools/agent/boot_stack.sh` +
`seed_org.py`), attach a source to a report, mark its connection unhealthy
(`Connection.is_active = False`) or run the completion as a user who isn't a
member of the source, then submit a prompt. Secrets (LLM keys) via env only.

## Proposed fix (NOT applied — root-cause report only)

Per the request, no code was changed. The fix belongs at the **divergence**, not
by hiding the source from the picker:

1. **Make the two gates agree.** Either have the report/selector serialization
   also account for connection health / run-user access (so an un-queryable
   source is not shown as an available agent), **or** stop silently discarding a
   report data source at execution time.
2. **Surface the drop instead of swallowing it.** When `_has_client`
   (`agent_v2.py:313-320`) or the `403`/health skips in
   `completion_service.py:359-363` remove a source the report still lists, emit a
   user-visible signal ("*Music Store* is temporarily unreachable") rather than
   letting the agent claim it has no data sources at all — today the removal is
   only a `logger.warning`.
3. **Distinguish "unhealthy" from "gone".** A transiently-failed connection
   health flag should not read to the model as "no data source connected";
   consider keeping the schema/context (with an unhealthy annotation) so the run
   degrades gracefully.

## What this proves / regression notes

The loop demonstrates that a report data source's presence in the **prompt box /
report** (`is_execution_live`) and its presence in the **agent context**
(`construct_clients` → `_has_client`) are governed by independent filters that
can disagree, and pins connection-health as one concrete trigger. The test
asserts the general invariant (live-to-selector but no-client ⇒ dropped by the
agent), not a single scripted scenario, so it survives as a regression guard for
whichever fix reconciles the two gates.
