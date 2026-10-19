# Feedback Loop — "records: [] should be non-empty", repeated for 30+ minutes

A list-filling chat (Hebrew contracts, SharePoint documents) saved its first
batch, then issued `submit_<list>` with `records: []` over and over, each time
rejected with `records: [] should be non-empty`, until a person stopped the
run after 32 minutes. Asked why, the agent said it had started sending empty
saves on the assumption that the system would "restore" records it had
already prepared. A second, separate error in the same run rejected a whole
batch because one field was `not_found` with text left in its value.

## Root cause (validated)

1. **Past saves replayed as empty saves.** `compact_replayed_args`
   (`backend/app/ai/context/replay_args.py`) shrank every successful
   `submit_list` call before it was replayed to the model, keeping only the
   sent `row_id`s. New rows are sent with `row_id: null`, so every save of new
   rows replayed as `{"records": [], "_replay_note": "…"}` next to a result of
   "3 inserted". The model reads a replayed call as exactly what it sent:
   * it copied it — an empty save (the reported loop), or
   * it concluded its own save was malformed and stopped (Loop B, `main`).
2. **No cap on failing saves.** `list` save failures are ordinary tool
   failures; the tool runner's 3-attempt cap only covers argument-validation
   failures. `AgentV2._batch_failure_rollup` exists but nothing calls it.
3. **Broken calls were turned into empty saves.** When the streamed tool
   arguments cannot be parsed or salvaged (e.g. cut off), the parser returns an
   `_unparsable` marker. `AgentV2._rewrite_list_action` read `records` from it,
   found none and sent `records: []`, so the tool runner's dedicated
   "your JSON was invalid" message and attempt cap never fired.
4. **`not_found` + leftover text rejected the batch.** `validate_records`
   rejected a `not_found` field whose value was not null; calls are atomic, so
   one slip lost every record in the call.

Hypothesis ruled out: an unescaped Hebrew quote (`בע"מ`) is **not** a path to
an empty save — the argument parser's salvage ladder already repairs it.

## Loop A — deterministic reproduction (no external services)

```bash
cd backend
export BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true
uv run pytest -q tests/unit/test_agent_list_replay.py tests/unit/test_agent_list_submit_guards.py
uv run pytest -q -m e2e --db=sqlite tests/e2e/rbac/test_agent_lists.py
```

Observed on `main` (8701a2b), before the fix:

```
FAILED test_agent_list_replay.py::test_a_saved_call_replays_as_the_save_it_was[records0..2]
FAILED test_agent_list_replay.py::test_successful_submission_args_are_compacted (+3 replay tests)
FAILED test_agent_list_submit_guards.py (rewrite: ('submit_list', {'list_id': 'L1', 'records': []}))
FAILED test_agent_lists.py::test_not_found_with_a_leftover_value_is_saved_as_not_found[10|not found|לא נמצא]
```

## Loop B — live confirmation (Claude Haiku 5.5, Music Store demo)

Backend only, driven through the API; key via `ANTHROPIC_API_KEY` env only.
One list (`artist` key, `track_count`, `top_genre`), two turns asking for the
top artists saved in batches of 3, one save per step.

| Run | Saves | Empty saves | Rejected saves | Rows saved | What the agent did |
|---|---|---|---|---|---|
| `main` | 1 | 0 | 0 | 3 | Read its own replayed save as "`records: []` plus an extra field", decided it was malformed, stopped and asked |
| Fix v1 (one `{"row_id"}` stub per record) | 2 | 0 | 0 | 3 | Read the stubs as "my save carried no field data", re-saved, stopped and asked |
| Fix v2 (this PR) | 7 | 0 | 0 | 21 | Saved batch after batch; no self-doubt about earlier saves |

Fix v1 was discarded because of this run: any shortening that changes what the
call says is read as what the model sent.

Cap wiring check: with the limits temporarily lowered to warn at 1 and stop at
2 (not committed), a forced-failure prompt ("list only accepts genre Polka,
retry unchanged until it succeeds") stopped after one rejected save instead
of the two seen with the stock limits, and explained the blocker to the user.
The hard stop itself was not reached live (Haiku stops on its own); it is
covered by `test_agent_list_submit_guards.py`.

## The fix

* `replay_args.py` — a saved call replays as the save it was: every record,
  row id, value and status, schema keys only; long quotes and notes are cut to
  60 characters. No `_replay_note`. Still deterministic, still at most half the
  size when quotes are long.
* `agent_v2.py` — `_rewrite_list_action` keeps the `_unparsable` marker;
  `list_save_failure_action` counts consecutive failed saves per list: a note
  on the 3rd ("fix exactly these fields, or stop and tell the user"), and the
  turn ends on the 5th with an honest final answer.
* `records.py` — `normalize_not_found`: `not_found` wins; a leftover value
  moves into `note`. Works on a copy; stored arguments are untouched.

## What this proves / regression notes

* Full unit suite: identical failing sets with and without the change (180
  each, all environment-related in this sandbox — e.g. missing `libodbc`);
  nothing new fails.
* `tests/e2e/rbac/test_agent_lists.py`: 67 passed (sqlite).
* BOW-103 stays fixed: `records` is always an array of real records, and the
  replay never adds a key the list schema forbids.
