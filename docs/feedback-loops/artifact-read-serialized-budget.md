# Feedback loop — artifact reads lose source before reaching the model

Large dashboard reads previously placed visualization profiles ahead of code;
the transcript's 100,000-character head cut removed the requested source. PR
#1470 first reordered the payload and corrected grep on long JSX lines. This
extension budgets the escaped JSON response and makes windowed reads lightweight.

## Root cause

`backend/app/ai/context/transcript.py:375` cuts serialized tool results without
regard to JSON structure. `backend/app/ai/context/result_parts.py:65` serializes
Unicode with JSON's default escaping. Raw character count therefore does not
predict model-visible response length. Even with source ordered first, a large
source range alone could overflow the transport cap.

## Reproduce and verify

From the repository's backend directory, with development dependencies installed:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db uv run pytest \
  tests/e2e/test_read_artifact_model_view.py \
  tests/unit/test_read_artifact_windowing.py \
  tests/e2e/test_doc_artifacts.py \
  tests/e2e/rbac/test_pentest_lows_authz.py -q --db=sqlite
```

Run the e2e files with `--db=postgres` as well. An existing throwaway PostgreSQL
server can be supplied through `TEST_DATABASE_URL`; no live customer DB is used.

Before the budget change, the updated full-read test failed because the rendered
response contained `[truncated at 100,000 of 209,549 chars]`. Bypassing the new
budget function made all four source/grep continuation tests fail. Restoring it
made those tests pass. Test data is synthetic, including Hebrew, Arabic and CJK.

Observed checks:

- SQLite: 76 passed across artifact model-view/windowing, docs and authorization.
- PostgreSQL: 38 passed across artifact model-view, docs and authorization.
- Unit/transcript compatibility: 127 passed.
- After reserving identity space dynamically: the 15 model-view tests passed
  again on both SQLite and PostgreSQL.
- Ruff on the new budget module and `git diff --check`: passed.

## Fix

`backend/app/ai/tools/artifact_read_budget.py` measures the same escaped JSON
representation as the transcript. It reserves identity and continuation space,
limits source payloads to 65K escaped characters, and keeps the entire visible
observation under 90K. Optional context is included only when it fits whole;
omissions are explicit. Pathological metadata returns an explicit error instead
of an apparently successful, truncated result.

Source windows remain verbatim. `next_read` contains line/character continuation
arguments, including for a single oversized line. Grep pages by match-window
offset, and oversized before/after context is explicitly omitted with range-read
guidance. Range and grep reads skip loading visualization profiles unless
`include_visualization_profiles=true`. Individual profiles remain available via
`read_query(visualization_ids=...)`. The audit/UI source window follows the same
budgeted source as the model observation.

## Bounds

No global JSON encoding or transcript cap changes. No changes to edit execution,
permission checks, or data-visibility policy. No live LLM edit or deployment was
performed. Existing full reads above 50K raw characters still return an outline;
follow-up ranges are budgeted. Images remain separate vision content and are not
charged against the text JSON cap.
