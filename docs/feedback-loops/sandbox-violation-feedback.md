# Feedback Loop — xls inspection dies repeatedly with "Security violation (unsafe_python): Forbidden function call: 'getattr()'"

Reported (mobile screenshot, July 2026): inspecting an uploaded Excel file made
`inspect_data` fail three times in a row, every attempt showing
`Security violation (unsafe_python): Code contains forbidden constructs:
Forbidden function call: 'getattr()'` — the second attempt contained *three*
`getattr()` calls instead of one. The claim validated here: the violation is
recorded everywhere **except** the one prompt that could fix it, so the coder
can never self-correct.

## Root cause (validated)

Four stacked gaps, all confirmed by reading code and by the live reproduction
below:

1. **The ban itself is correct.** `FORBIDDEN_BUILTINS`
   (`backend/app/ai/code_execution/code_execution.py`) rejects any `getattr`/
   `hasattr` call at AST level, because dynamic attribute lookup would bypass
   the dunder-attribute blocklist (`getattr(df, '__cl' + 'ass__')`). Not the bug.
2. **The coder was never told.** The inspection codegen prompt
   (`Coder.generate_inspection_code`) mentioned the `getattr` ban only inside
   the HTTP/`FetchedPage` bullet — invisible for an Excel task. Excel is also
   exactly where models reach for `getattr(col, 'strip', None)`-style defensive
   code (messy mixed-type headers, `header=None` reads).
3. **Violations were terminal.** `generate_and_execute_stream_v2` `break`s on
   any `CodeSecurityError`, even though `unsafe_python` fires **before**
   `exec()` — nothing has run, so there is no safety reason not to retry.
4. **Feedback was dropped at the last step.** The violation *is* recorded in
   the planner's observations and even plumbed into `CodeGenContext`
   (`past_observations` / `last_observation`), and the executor passes
   `code_and_error_messages` into every codegen call — but neither
   `generate_inspection_code` nor the v2 `generate_code` prompt template ever
   interpolated them (`generate_code` section 4 told the model to "review the
   code_and_error_messages" that were not in the prompt). Each regeneration was
   a blind re-roll, so the same idiom came back.

## Loop A — deterministic reproduction (no external services)

`backend/tests/unit/test_sandbox_feedback_loop.py` drives the real executor
with a stubbed `code_generator_fn` (LLM is the only stubbed boundary):

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
TESTING=true uv run pytest tests/unit/test_sandbox_feedback_loop.py -q
```

Observed on pre-fix code — 8 FAIL / 2 pass:

- `test_violation_then_clean_code_succeeds` — executor stopped after one
  attempt (`calls == 1`), `df is None`.
- `test_retry_receives_violation_feedback` — no second codegen call existed.
- `test_inspection_prompt_contains_previous_error` /
  `test_create_data_prompt_contains_previous_error` — the violation text never
  appeared in the rendered prompt.
- `test_*_prompt_lists_forbidden_builtins` — prompts did not state the
  sandbox's forbidden-builtin list.
- `test_failed_last_observation_rendered` — a failed previous tool run was not
  surfaced to the next inspection prompt.

## Loop B — live confirmation (real Anthropic key, full stack)

Stack via `tools/agent/boot_stack.sh` + `tools/agent/seed_org.py`; Anthropic
provider registered through `POST /api/llm/providers` with the key from the
environment (env var only, never committed). A messy two-sheet
`clients_ledger.xlsx` (banner rows, mixed-type header labels: str/int/float/
datetime/None, a `DMCH` client-code column) is uploaded through
`POST /api/files`, attached to a report, and a real completion is run.

Pre-fix, a prompt steering the coder toward the `getattr` convention produced
exactly the reported failure, recorded in SQLite (`db/agent.db`):

- `tool_executions`: `inspect_data` row with `status=error`, `success=0`,
  `error_message = Security violation (unsafe_python): … 'getattr()'`, and the
  generated code containing `file_path = getattr(excel_file, 'path', None)`.
- `audit_logs`: `security.unsafe_code_blocked` + `tool.data_query_failed`.
- One attempt only (`retries=1` at the time); the planner had to route around
  the tool.

## The fix

- `backend/app/ai/code_execution/code_execution.py` — `unsafe_python`
  violations now consume a retry (with the violation appended to
  `code_and_error_messages`) instead of ending the run; the
  `security_violation` audit event is still emitted. `unsafe_sql` stays
  terminal: it fires mid-execution, so the attempt is not safely repeatable.
- `backend/app/ai/agents/coder/coder.py` —
  - `_sandbox_rules_section()` renders the forbidden builtins/modules into both
    v2 codegen prompts, **derived from the validator's own frozensets** so
    prompt and enforcement cannot drift, with the Excel-specific substitute
    (`str(col).strip()`, never `getattr(col, 'strip', …)`).
  - `_render_error_feedback()` interpolates the last failed (code, error)
    pairs into both `generate_code` and `generate_inspection_code` — the
    phantom "review the code_and_error_messages" instruction now has real
    content behind it.
  - `_render_last_failed_observation()` surfaces a failed previous tool run
    (from `context.last_observation`) in the inspection prompt, covering the
    planner-re-invokes-the-tool loop from the report.
- `backend/app/ai/tools/implementations/inspect_data.py` +
  `backend/app/ai/tools/mcp/inspect_data.py` — `retries` 1 → 2; a retry is no
  longer a blind re-roll, so the extra attempt buys a correction.

Re-running Loop A: **10 passed**. Re-running Loop B with the same
getattr-steering prompt: `inspect_data` `success=1`, final code free of
`getattr`, correct DMCH output, and **no** `security.unsafe_code_blocked`
audit row — the prompt rules prevent the idiom at generation time, and the
executor retry stands behind it if a model still emits one.

## What this proves / regression notes

- Prevention layer: both codegen prompts now state the sandbox rules, kept in
  sync with the validator by construction (test fails if a builtin is added to
  the validator but missing from the prompt).
- Correction layer: a violation feeds the exact code + error back into the
  next attempt within the same tool call; budget still bounds the loop
  (`test_budget_still_bounds_violations`).
- Audit behavior unchanged (`security.unsafe_code_blocked` still logged per
  violation).
- `tests/e2e/test_loadables.py` (16 tests over the same executor loop) passes
  unchanged.
- Live note: on the deliberately messy sheet, inspection can still fail for
  ordinary data reasons (e.g. `KeyError: 'Client Code'` twice) — it now
  retries with feedback and the planner falls back to another tool; that is
  the intended failure economics, not this bug.
