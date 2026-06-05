# SSE create_data payload bloat

## Context

Customer reports around parallel prompts led us to benchmark SSE payload size and server resource usage.

The first optimization already shipped locally and was hotpatched on the instance:

- Commit: `db3b855c Reduce planner SSE partial bloat`
- Change: `decision.partial` now emits only action metadata. Cumulative reasoning/assistant/final-answer text streams through `block.delta.token` / `block.delta.text`.

Measured effect on the data-source schema prompt:

- Before: `1,985,170` SSE bytes; `decision.partial` was about `1.65MB`.
- After: `252,332` SSE bytes; `decision.partial` was about `981B`.
- Total reduction: about `87%`.

## Current benchmark

Prompt:

```text
show me list of albums
```

Data source:

```text
db71a871-a2d5-42c4-baf8-bb99d9331f1d
```

Important: run the benchmark client from the local machine. Use SSH only to observe the EC2 instance.

Local-origin benchmark result after the `decision.partial` fix:

- Report: `37572a6f-58a7-4bef-960d-4373e56dfd7c`
- HTTP status: `200`
- Wall time: `19.97s`
- First SSE chunk: `3.96s`
- SSE size: `169,561` bytes
- Events: `76`
- Tool: `create_data`
- Created widget: `Album List`
- Created step: `Album List`, `success`
- Output data: `347` rows x `4` columns
- Query time: `10.5ms`
- Codegen time: `7.36s`
- Execution time: `69ms`

Resource samples during the run:

- `bow-app`: max CPU `92.81%`, avg CPU `44%`, max memory `855.5MiB`
- `bow-postgres`: max CPU `24.3%`, avg CPU `7.61%`, max memory `304.9MiB`
- Post-run DB activity looked clean: no `idle in transaction` buildup.

Event byte profile:

| Event | Count | Bytes | Percent |
| --- | ---: | ---: | ---: |
| `block.upsert` | 5 | 94,078 | 55.5% |
| `tool.finished` | 1 | 46,308 | 27.3% |
| `block.delta.token` | 35 | 11,187 | 6.6% |
| `tool.progress` | 11 | 4,952 | 2.9% |
| `instructions.context` | 1 | 4,114 | 2.4% |
| `decision.partial` | 2 | 1,249 | 0.7% |

## Root cause

The remaining bloat is no longer text streaming. It is `create_data` result payload duplication.

For the album prompt:

- `tool.finished.result_json.data.rows` carried about `39KB` of row data.
- The following `block.upsert` embedded `tool_execution.result_json.data`.
- The same `block.upsert` also embedded `tool_execution.created_step.data`.

So the same table-sized data is effectively streamed multiple times.

Relevant backend paths:

- `backend/app/ai/agent_v2.py`
  - Emits `tool.finished`.
  - Injects `query_id` into `result_json` for frontend hydration.
- `backend/app/serializers/completion_v2.py`
  - Serializes `ToolExecutionUISchema`.
  - Already strips `widget_data`, but does not strip `result_json.data`.
- `backend/app/ai/tools/implementations/create_data.py`
  - Produces full `output.data`, plus `data_preview`, `stats`, `code`, `data_model`, `view`, timings, and query metadata.

## What the client uses

The report page applies streaming events in `frontend/pages/reports/[id]/index.vue`.

For `block.upsert`, it merges the incoming block into `sysMessage.completion_blocks`.

For `tool.finished`, it assigns:

- `payload.result_json` to `block.tool_execution.result_json`
- `payload.created_widget_id`
- `payload.created_step_id`
- `payload.created_visualization_ids`
- `payload.duration_ms`
- `payload.result_summary`

`CreateDataTool.vue` uses:

- `created_step.code`, falling back to `result_json.code`
- `result_json.errors`
- `result_json.stats.total_rows`
- `result_json.execution_ms`
- `duration_ms`
- `result_json.data_model`
- `result_json.view`
- `result_json.data.rows` only as a fallback for row count/preview

`ToolWidgetPreview.vue` uses:

- `created_step` first for rendering the preview table/chart.
- `result_json.data` only as a fallback if `created_step` is absent.
- `result_json.query_id` to hydrate the latest default step through `/api/queries/{query_id}/default_step`.

This means full rows do not need to be carried in both `result_json.data` and `created_step.data`.

## Recommended fix

### Phase 1: safe backend trimming

Trim the SSE copy of `tool.finished.result_json` for `create_data`.

Keep:

- `success`
- `code`
- `data_preview`
- `stats`
- `errors`
- `data_model`
- `view`
- `executed_queries`
- `query_timings`
- `codegen_ms`
- `execution_ms`
- `query_id`
- `created_visualization_ids`

Drop from the SSE payload:

- `result_json.data`

Do not change DB persistence yet. Persisted `ToolExecution.result_json` can stay rich until the rest of the system is updated.

Expected impact for album benchmark: remove about `39KB` from `tool.finished`.

### Phase 2: strip duplicate data from block.upsert

In `backend/app/serializers/completion_v2.py`, extend the existing heavy-payload stripping:

- For `create_data`, strip `tool_execution.result_json.data`.
- Keep `result_json.data_preview`, `stats`, `data_model`, `view`, and `query_id`.

Expected impact for album benchmark: remove another about `39KB` from `block.upsert`.

After phases 1-2, the album benchmark should fall from about `170KB` to roughly `80-90KB`.

### Phase 3: compact created_step in streaming block.upsert

For an even smaller stream, avoid sending full `created_step.data.rows` in live SSE.

Keep a compact step shape in SSE:

- `id`
- `title`
- `slug`
- `status`
- `query_id`
- `code`
- `data_model`
- `view`
- optional `data_preview`

Then let `ToolWidgetPreview.vue` hydrate full data using the existing route:

```text
GET /api/queries/{query_id}/default_step
```

This needs client hardening so the preview renders from `data_preview` while hydration is in flight, then swaps to the hydrated step.

Expected impact after phase 3: album stream likely falls toward `40-60KB`, depending on metadata size.

### Phase 4: update model/context consumers

Longer term, `Step.data` should be the canonical full-row store. `ToolExecution.result_json` should hold metadata and preview, not another full data copy.

Before making that persistent change, update `backend/app/ai/context/builders/message_context_builder.py`.

It currently summarizes `create_data` from `tool_execution.result_json.data`. It should instead use:

- `result_json.stats.total_rows`
- `result_json.data_preview.columns`
- linked `created_step_id` if full data is truly required

## How to check

### 1. Verify the app still streams successfully

Run the benchmark from the local machine, not from the EC2 instance.

Use the same account, data source, and prompt. Do not commit credentials into the repo.

The benchmark should:

1. Login.
2. Fetch organizations.
3. Create a fresh report with:

```json
{
  "title": "Load bench albums ...",
  "widget": null,
  "files": [],
  "data_sources": ["db71a871-a2d5-42c4-baf8-bb99d9331f1d"]
}
```

4. POST a streaming completion:

```text
POST https://app.bagofwords.com/api/reports/{report_id}/completions?stream=true
Accept: text/event-stream
```

Body:

```json
{
  "stream": true,
  "prompt": {
    "content": "show me list of albums",
    "widget_id": null,
    "step_id": null,
    "mentions": [],
    "mode": "chat"
  }
}
```

5. Save raw SSE to `/tmp/bow-albums-{report_id}.sse`.

Expected success checks:

- HTTP status is `200`.
- Completion statuses for the report are `success`.
- A widget named `Album List` exists.
- A step named `Album List` exists and has status `success`.
- The result has about `347` rows and `4` columns.

### 2. Check SSE event sizes

Parse the raw SSE by splitting on blank lines, reading `event:` and `data:` fields, and summing encoded block sizes by event.

Track at least:

- Total SSE bytes
- `block.upsert` bytes
- `tool.finished` bytes
- `decision.partial` bytes
- Event counts

Expected targets:

- Before create-data trimming: about `170KB` total for the album prompt.
- After phases 1-2: roughly `80-90KB`.
- After phase 3: roughly `40-60KB`.
- `decision.partial` should stay near `1KB`, not regress to megabytes.

### 3. Check duplicate payloads are gone

Inspect the saved SSE:

- `tool.finished.data.result_json.data` should be absent after phase 1.
- `block.upsert.data.block.tool_execution.result_json.data` should be absent after phase 2.
- `block.upsert.data.block.tool_execution.created_step.data.rows` should be absent or preview-sized after phase 3.
- `query_id` must still be present in `tool_execution.result_json` or in `created_step`.

### 4. Check client behavior

In the browser, run the same prompt and verify:

- The create-data tool shows generated code.
- The row count displays.
- The preview table/chart appears.
- The preview hydrates to the full step after completion.
- Edit query still opens with the correct query/step.
- Refreshing the report still shows the created widget/step.

### 5. Check server health

While the local benchmark is running, observe EC2 over SSH:

```bash
ssh bow 'sudo docker stats --no-stream --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.PIDs}}"'
```

Check DB activity after the run:

```bash
ssh bow 'sudo docker exec bow-postgres psql -U bow -d bagofwords -c "select state, count(*) from pg_stat_activity group by state order by state nulls last;"'
```

Check logs:

```bash
ssh bow 'sudo docker logs --since 10m bow-app 2>&1 | egrep -i "ERROR|Traceback|UNAVAILABLE|Failed to detach|localhost:4317" | tail -80 || true'
```

Expected:

- No `localhost:4317` exporter errors.
- No completion failures.
- No `idle in transaction` buildup.

Known residual issue:

- `opentelemetry.context Failed to detach context` has appeared around stream runs. This is separate from the Jaeger endpoint fix and should be tracked as a reliability follow-up.
