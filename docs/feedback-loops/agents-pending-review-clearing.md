# Agents page — "Pending review" badges survive reject-all (and refresh)

Reported flow: on the **Agents** page a reviewer rejects every suggested change,
yet the amber **Pending review** row badges and the **"N pending"** chip keep
showing. Refreshing the page doesn't clear them either — for some rows nothing
can.

## Root cause

Two layers, both downstream of the deliberate perf trade documented in
`agents-pending-reconciliation-perf.md`: the badge surfaces
(`GET /instructions/counts`, `GET /instructions/pending-changes`, the
`pending_only` list) answer from the **non-diffing** tier of
`get_pending_change_instruction_ids(verify=False)`, which reports a *drifted*
suggestion (its base no longer matches main) as pending **optimistically**,
without confirming any hunk survives the rebase.

1. **Backend — the badge nobody could clear.** `hunks/reject-all` (and the
   other per-hunk resolutions) only record resolutions for hunks that are
   currently **live**. A drifted suggestion whose change is already contained
   in main rebases to *zero* live hunks — the review pane rightly shows nothing
   to review — so reject-all recorded **nothing** for it. With no recorded
   resolution, the optimistic sweep kept reporting the row as pending on every
   page load, forever: not clearable by rejecting, not by refreshing.

2. **Frontend — the clear that un-cleared itself.** Opening an instruction runs
   the authoritative per-hunk pass (`/review-hunks`); when it comes back empty,
   `KnowledgeExplorer.loadPending` cleared the row's dot locally. But every
   mutation then runs `refreshLists → fetchCounts`, which **replaces**
   `pendingInstrIds` wholesale with the server's optimistic set — resurrecting
   the badge the user just watched clear.

## Fix

- **Settled markers** (`instruction_service.py`). When a per-hunk resolution
  (accept/reject, single or all) leaves a suggestion build with **no live,
  unrejected hunk** for the instruction, `_settle_resolved_suggestion_rows`
  stamps a `__settled__` marker into the build's `rejected_hunks` metadata,
  bound to the exact `(main version, proposed version)` pair it was verified
  against. The sweep (both tiers) and `review_hunks` treat a matching marker as
  a conclusive, **zero-diff** "not pending"; if main drifts again or the build
  stages a new proposed version, the marker stops matching and the row is
  re-evaluated like any other. The marker is review metadata only — the build's
  content snapshot is untouched, so publish/merge semantics don't change (a
  `remove_from_build` here would have read as "deleted" if e.g. a git build was
  later published wholesale). As a bonus, fully-rejected drifted rows no longer
  pay the quadratic exact check on every counts call.

- **Sticky authoritative verdicts** (`KnowledgeExplorer.vue`). Rows the
  authoritative pass proved empty are remembered in `verifiedNotPending` and
  excluded from `isPending`, the "N pending" chip and the Pending-changes list
  even after `fetchCounts` overwrites the optimistic set; a row leaves the set
  the moment the authoritative pass finds a real pending change again. The
  Pending-changes flat list also refetches after mutations while it is open.

## Tests

`backend/tests/e2e/test_instruction.py`:

- `test_reject_all_clears_pending_badges_without_refresh` — the reported flow:
  after reject-all, counts / sweep / pending_only all agree, nothing for a
  refresh to bring back.
- `test_reject_all_settles_drifted_noop_suggestion` — the previously unkillable
  badge: drifted suggestion, zero live hunks, optimistically pending; verified
  to FAIL before the settle marker and pass after.
- `test_partial_reject_keeps_pending_badges` — guard against over-settling:
  one of two hunks rejected keeps every surface pending; rejecting the last
  hunk settles everywhere.
