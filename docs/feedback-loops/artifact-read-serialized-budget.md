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

## Review follow-up

Five review findings, each with a negative control:

- **The continuation note replaced the read's own summary.** `summary` is the
  only field surviving tier decay for this tool (`read_artifact` declares no
  `digest_keys`, so `digest_of` falls back to summary plus the reference ids),
  so a paged read told the planner only that something was partial — not which
  artifact, which lines, or how many matches. `_append_note` now adds to it.
- **A grep page could repeat the call that produced it.** When the first match
  did not fit even with its context stripped, the page came back empty while
  reporting more to come, with `next_read.match_offset` equal to the incoming
  one. A page now always carries its first match, and the floor
  (`GREP_MATCH_MAX_CHARS`) guarantees room for one, derived from
  `GREP_LINE_CLIP` at 12 escaped chars per astral-plane character. The floor
  applies to grep only: a code or outline read is prefix-trimmed and safe at
  any width.
- **The transport contract was copied, not referenced.** `METADATA_KEYS` now
  comes from `result_parts`, and the ceiling is derived from
  `transcript.MAX_RESULT_CHARS` (newly named) minus `MODEL_READ_HEADROOM`.
- **`invalid_character_offset` yielded `output: {}`** where every other branch
  yields a full `ReadArtifactOutput`; the UI card and audit row read those
  fields.
- **The audit row kept source on the metadata-error path**, contradicting the
  "same source window" invariant one line below it.

Verification:

```sh
TESTING=true ENVIRONMENT=production uv run pytest \
  tests/unit/test_artifact_read_budget.py \
  tests/unit/test_read_artifact_windowing.py \
  tests/e2e/test_read_artifact_model_view.py -q --db=sqlite
```

86 passed. Reverting each fix in turn fails the matching test: the four summary
cases plus the tool-level summary test, the grep-progress sweep across the
budget band where one astral match does not fit, and the output-shape test.
`tests/unit` at this head and at base `c824abc` fail the identical set of
pre-existing tests (87 in a sandbox missing optional data-source drivers; 7 of
them are what CI's `unit-tests` job reports, none in files this PR touches).

## Bounds

No global JSON encoding or transcript cap changes. No changes to edit execution,
permission checks, or data-visibility policy. No live LLM edit or deployment was
performed. Existing full reads above 50K raw characters still return an outline;
follow-up ranges are budgeted. Images remain separate vision content and are not
charged against the text JSON cap.
