# Feedback Loop — exact, durable instruction review

## User-visible failure

A reviewer opened one long instruction, saw one small pending change, and clicked
**Accept all**. The next render showed dozens of deletions that the reviewer had
never seen or approved. Accept/reject was therefore acting on a different change
set from the one represented in the UI.

## Root cause (validated from the live SQLite history)

Three independent gaps stacked:

1. For documents above the diff cap, `text_hunks.py` represented any edit as one
   whole-document hunk. This made a two-word edit look like a full replacement
   and made the decision key describe the whole document rather than the edit.
2. The WYSIWYG editor split Markdown on every blank line before parsing each
   fragment. Blank lines inside fenced blocks were treated as document
   boundaries, so MarkdownIt auto-closed/reopened the block. One later edit could
   serialize hundreds of synthetic empty fences into a new version.
3. Bulk accept/reject did not submit the hunks shown on screen. The server
   rediscovered every pending build at click time, and settlement was tied to the
   then-current main version. A previously reviewed proposal could therefore
   reappear after main advanced and be included in a later **Accept all**.

The database history confirms the mechanism rather than just the symptom:

- the intended version was the prior version plus exactly `\nhello world\n`
  (13 bytes);
- the bad accept promoted text identical to an older stale proposal;
- its diff contained 52 blocks, including deletion of 440 synthetic standalone
  fences;
- that stale proposal had already been reviewed against an older main, but its
  settlement no longer matched after main moved, so it resurfaced.

## Correctness contract

The review mutation now has one explicit contract:

- `GET /review-hunks` returns an immutable main-build id, main-version id, and
  canonical `(build_id, hunk_key)` action units.
- The client submits exactly the visible, non-overlapping action units.
- The server recomputes those exact keys against that exact main snapshot. A
  missing, duplicate, overlapping, ambiguous, or moved hunk returns conflict and
  writes no review decision.
- Promotion claims main with compare-and-swap semantics. A concurrent main
  promotion makes the review fail closed.
- Accepted/rejected decisions are written only after the exact build/version is
  confirmed live.
- Settlement belongs to the immutable proposed version. Later edits to main do
  not resurrect a proposal that was already accepted or rejected; changing the
  proposal version does reopen it.

Long documents stay granular: the matcher trims exact common prefix/suffix and
diffs only the changed middle. Old oversized decision keys remain recognized so
historical accepted/rejected proposals do not reappear.

The editor now parses Markdown as one stream and applies only TipTap's normalized
delta back onto the exact source Markdown, preserving untouched whitespace and
fence formatting.

## Deterministic regression loop

```bash
cd backend
env UV_CACHE_DIR=/private/tmp/bow-uv-cache uv run pytest \
  tests/unit/test_text_hunks.py \
  tests/e2e/test_instruction.py \
  tests/e2e/test_instruction_stale_base.py \
  tests/e2e/test_instruction_review_events.py \
  tests/e2e/test_training_agents_page_roundtrip.py \
  tests/e2e/test_instruction_verdict_and_echo.py \
  tests/e2e/rbac/test_instruction_pending_carryover.py -q

cd ../frontend
npm run build
```

The key regression creates a long, repetitive instruction, stages one displayed
hunk, accepts it with the returned snapshot tokens, and asserts that only those
bytes changed. Companion cases cover stale main snapshots, rejected/accepted
proposal durability, legacy oversized keys, pending-badge agreement, and the
report/training call sites.

## Live localhost proof

The final pass used the real dev API and `backend/db/app.db`, not a mocked
transport:

1. Opened the reported page in the app and confirmed its corrected
   `review-hunks` response has no resurrected proposal and the UI renders no
   change count or bulk-review buttons.
2. Created a throwaway published instruction through the localhost API with 700
   repetitive sections and 1,400 Markdown fences.
3. Staged one pending proposal adding only `hello world\n\n`.
4. Confirmed the API rendered exactly one hunk, then submitted its exact snapshot
   to `accept-all`.
5. Confirmed the published text equaled the proposal byte for byte: +13 bytes,
   with every fence preserved.
6. Confirmed the accepted proposal did not reappear.
7. Replayed the stale payload and received HTTP 409; the published text remained
   unchanged.
8. Deleted the throwaway instruction.

Observed result:

```json
{"accepted_exactly":true,"accepted_proposal_resurfaced":false,"bytes_added":13,"displayed_hunks":1,"markdown_fences_preserved":true,"stale_replay_status":409,"target_open_suggestions":0,"throwaway_instruction_deleted":true}
```

The historical instruction content itself is deliberately not rewritten by this
fix. Repairing an already-promoted old version is a separate data-restoration
decision; the mutation path now prevents the same corruption from recurring.
