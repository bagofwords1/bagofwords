# Feedback Loop — `bow.tool_calls` returns the code a call used

**Status: IMPLEMENTED and VERIFIED (2026-10-29).** Loop A passes on SQLite. Loop B ran the real
backend with a **scripted** OpenAI-compatible model. A run with a real model was not possible in
this sandbox (the Anthropic key had no credit), so whether a real planner chooses to request `code`
is not covered here.

Question: *"In `bow.runs`, do we also get the code used?"* No. Neither BOW dataset exposed it:
`bow.runs` has run-level fields only, `bow.tool_calls.args_preview` holds the `create_data` input
(title, prompt, tables, written before any code exists), and `output_preview` is a one-line summary.
The code lived only in `Step.code` (and in the encrypted `ToolExecution.result_json`).

## The change (option A: join the created step)

| Piece | Where |
|---|---|
| `code` field on `bow.tool_calls` = `Step.code` via `ToolExecution.created_step_id` (outer join only when requested) | `backend/app/services/bow_source_service.py` (`TOOL_FIELDS`, `query`) |
| Opt-in: not in `DEFAULT_COLUMNS`; row column only (group_by / metrics on it raise) | same file, `ROW_ONLY` |
| Capped at 20,000 characters (`MAX_CODE_CHARS`) | same file |
| Withheld (null) wherever the step's rows are withheld from the reader — same boundary as `query_service` / `read_query`, decided by `resolve_step_data` once per step | `_redact_code` in the same file |
| Coder guidance | `backend/app/data_sources/clients/bow_client.py` (`BowClient.description`) |
| Train-agent skill pointer | `backend/app/ai/skills/library/train-agent.md` §5 |
| Seed fixture accepts `step_id` on tools | `backend/tests/fixtures/console_metrics.py` |

Semantics: `code` is the step's **current** code. A later rerun/edit of that step replaces it. Calls
that created no step have `code = null`.

## Loop A — deterministic

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" TESTING=true
uv run pytest tests/e2e/rbac/test_bow_source.py -m e2e --db=sqlite -q
```

Observed: `32 passed` (5 new + 27 existing). New tests create a real step through `/api/queries/{id}/run`
and link a seeded `create_data` call to it:

- admin and agent manager both get the step's code, capped at 20,000 chars; a call with no step gets
  null; the default projection has no `code`;
- after the agent's connection is switched to `user_required` (viewer identity), the manager (not the
  report owner) gets null while the owner still gets the code;
- `group_by: ["code"]` and `count_distinct` on `code` raise.

Can it fail? With the withholding decision forced to `False`, the withholding test fails:
`assert (2 == 2 and False)` — the manager received the SQL.

Before the change, any request for `code` raised `Unknown tool_calls columns: code`.

## Loop B — real backend, scripted model

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/sbx_codecol.db BOW_ENCRYPTION_KEY=<stable fernet key>
uv run alembic upgrade head && uv run uvicorn main:app --port 8000 &
uv run python ../tools/agent/seed_org.py --demo --db-path db/sbx_codecol.db
STUB_DS_ID=<Music Store id> STUB_LOG=/tmp/codegen.log \
  uv run python ../docs/feedback-loops/2026-10-29-bow-tool-calls-code.stub_llm.py &
uv run python ../tools/agent/setup_stub_llm.py
uv run python ../docs/feedback-loops/2026-10-29-bow-tool-calls-code.drive.py
```

The driver asks a chat question on the Music Store agent (the model's `create_data` runs SQL and
saves a step), then a training-mode question whose `create_data` targets `builtin:bow` /
`bow.tool_calls` with `columns: [..., "code"]`.

Observed:

```
chat create_data calls: [('3b022d61', 'success', '338f8a8a')]
training create_data: success step 5d9440c7
training table columns: ['call_id', 'run_id', 'created_at', 'tool', 'status', 'step_id', 'code']
rows: 15 row for the chat call: True
code returned == chat step code: True
--- code returned for the chat call ---
def generate_df(ds_clients, excel_files):
    key = [k for k in ds_clients if k != "bow"][0]
    return ds_clients[key].execute_query(
        "SELECT g.Name AS genre, COUNT(*) AS tracks FROM Track t "
        "JOIN Genre g ON g.GenreId = t.GenreId GROUP BY g.Name ORDER BY tracks DESC")
```

The coder's prompt (captured in `STUB_LOG`) contains the new guidance line
`tool_calls has code: the code of the step the call created (create_data), not in the defaults...`.
The training table was saved as an ordinary step on a BOW-protected report, like any `bow.*` result.

## Not covered

- A real model deciding to request `code` (no LLM credit in this sandbox).
- Postgres (CI runs it; the change is a plain outer join plus Python post-processing).
- Exact per-attempt code (option B, from `ToolExecution.result_json`) — deliberately left out.
