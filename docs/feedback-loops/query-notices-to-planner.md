# Query notices reach the planner as `data_notices`

## Problem

Connectors attach a note to a result frame with `df.attrs["bow_notice"]`
(`QUERY_NOTICE_ATTR`, `backend/app/ai/code_execution/code_execution.py`).
Neo4j sets one when the row limit cuts a result off; Priority, MECM and others
set similar ones. The note was only printed into the run log
(`execution_log`). The create_data **success observation** never included that
log, so the planner saw "5 rows x 1 cols" and nothing else. In a live run
(Haiku, Neo4j row limit 5, 30 Person nodes) the agent answered "There are 5
people in the graph… the result is not truncated."

## Fix

- `QueryCapturingClientWrapper` (parent side, so both sandboxed and in-process
  runs) copies each result's note onto that query's timing record as `notice`
  (max 1000 chars). `collect_query_notices(query_timings)` returns the distinct
  notes in order.
- `app/ai/tools/data_notices.py::attach_data_notices` puts them on the
  observation as `data_notices` and adds a pointer to `summary`. Used by
  `create_data` and `inspect_data` (on success). `mcp.create_data` returns them
  as `MCPCreateDataOutput.data_notices`, since MCP clients never got the log.
- `data_notices` is in `_OBS_KEEP_KEYS`, so it survives past-observation
  minification.
- Planner v3 COMMUNICATION rule: a result with a row-limit notice is never "all"
  or "complete"; say it was limited, or re-query.

Notes are not redacted for viewers without code access. They were already shown
unredacted in `execution_log`.

## Verification

- `backend/tests/unit/test_query_notice_observation.py` (9 tests) runs a generic
  row-capped stub connector through the real executor in both sandbox modes and
  checks the observation. 7 of them fail with the fix removed.
- Neighbouring suites pass (sandbox, failure observation, neo4j client, code
  visibility, compaction, planner prompt and MCP tests).
- **Not done yet:** the live check that the agent stops claiming completeness.
  The A/B script (Haiku, same request, old vs new observation) could not run
  because the sandbox's Anthropic account had no credit. Re-run it with the
  sandbox-feedback-loop skill against a Neo4j connection with row limit 5.
