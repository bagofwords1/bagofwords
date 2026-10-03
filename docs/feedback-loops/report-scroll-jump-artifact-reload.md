# Feedback Loop — "the page jumps when I start scrolling" / "the artifact refreshes a few times on load"

Two reports against the report page, mostly on mobile:

1. Content loads, the user starts scrolling the chat, and the view **jumps**
   back down.
2. The dashboard (artifact iframe) **refreshes several times** right after
   the page opens.

Both reproduce deterministically against a seeded sandbox report.

## Root cause (validated)

### 1. Chat scroll jump

Follow mode on the chat is positional (`isFollowing`, updated by `onScroll` in
`frontend/pages/reports/[id]/index.vue`). Three paths broke it:

- **Forced catch-up burst.** `scheduleInitialScroll` called
  `forceScrollToBottom` at 0/80/160/320/640 ms. A forced scroll ignores and
  re-enables follow mode, so a reader who had started scrolling up ~300 ms
  after content appeared was pulled back to the bottom by the 640 ms timer.
  Measured: the jump lands at `scheduleInitialScroll + 640ms + nextTick + 40ms`
  in every run, from `scrollToBottom`'s timer.
- **Late listener.** `onScroll` was attached with `addEventListener` as the
  last line of `onMounted`, after every load await. The chat renders before
  that, so for the whole load window the reader's scrolling was untracked and
  every follow-scroll yanked them.
- **Mobile tab switch.** The chat is under
  `v-if="!isMobile || mobileView === 'chat'"`; Dashboard → Chat recreates the
  scroll container, the listener stayed on the old element, and the chat came
  back at `scrollTop = 0`. With follow mode frozen, the `resize` events mobile
  browsers fire as the URL bar hides snapped the reader to the bottom.

### 2. Dashboard reloads

`ArtifactFrame.vue` paints first, then `POST /api/r/{id}/rerun`
(refresh-on-view). When that rerun ran, it called `refreshAll()` →
`fetchData()`, which sets `dataReady = false` (srcdoc → empty: the iframe
reloads **blank**) and freezes a new `srcdocSeed` (srcdoc changes again: a
**third** load). The server gates the rerun to once per 5 minutes per report
(`REFRESH_ON_VIEW_MIN_INTERVAL_SECONDS`), hence "sometimes".

## Loop A — deterministic reproduction

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
# one fresh report per measured load (refresh-on-view is rate-limited per report)
R=$(TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db \
    uv run python ../tools/agent/seed_long_report.py | grep report_id | jq -r .report_id)
cd ../frontend
node ../tools/agent/repro_report_scroll_reload.mjs $R mobile 200   # or: desktop
node ../tools/agent/repro_report_tab_scroll.mjs $R                 # fresh $R
```

`repro_report_scroll_reload.mjs` (390×844 touch viewport) waits for the chat to
be scrollable, waits 200 ms, wheels up 1500 px like a reader, and logs every
scroll event, programmatic `scrollTop` write, iframe `srcdoc` set / `load` and
the rerun response. A "yank" is a scroll event during the gesture that moves
the view down by >100 px.

Observed on `main` (3 mobile runs + 1 desktop, fresh report each):

| | yanks during user scroll | iframe loads on open | blank loads |
|---|---|---|---|
| mobile ×3 | 1, 1, 1 | 3, 3, 3 | 1, 1, 1 |
| desktop | 0 | 3 | 1 |

Tab switch on `main`: chat returns at `top 0`; after scrolling up to 1915, a
viewport resize moves it to 3479 (the bottom).

## The fix

- `index.vue`: `@scroll.passive="onScroll"` on the chat container (tracked from
  first render, re-bound on every remount); `scheduleInitialScroll` forces only
  the first scroll and makes the catch-ups `followScrollToBottom`; a
  `mobileView` watcher snapshots scroll position + follow state when leaving
  Chat and restores it on return.
- `ArtifactFrame.vue`: after a successful refresh-on-view rerun,
  `refreshDataInPlace()` → `fetchData(id, { inPlace: true })`: no loading
  overlay, no `dataReady` reset, no new `srcdocSeed`, applied control values
  kept; the fresh rows reach the live document through the existing
  `visualizationsData` watcher → `postMessage`. Before first paint it takes the
  normal path.

Same loop after the fix:

| | yanks during user scroll | iframe loads on open | blank loads |
|---|---|---|---|
| mobile ×3 | 0, 0, 0 | 1, 1, 1 | 0, 0, 0 |
| desktop | 0 | 1 | 0 |

Tab switch after the fix: chat returns at the bottom (3415/3415); after
scrolling up to 1915 a resize leaves it at 1915.

Fresh data still arrives: after the in-place refresh the rendered dashboard
shows the rerun's `2024-01…` rows and none of the stale `2023-10` seed rows
(desktop and mobile).

## What this proves / regression notes

- The scroll loop covers the reported mobile scenario end to end; on desktop
  the split panel opens later, so the forced burst rarely overlapped a gesture
  and the old code showed no yank there.
- Not covered: real iOS Safari momentum scrolling and the real URL-bar resize
  (simulated with `setViewportSize`); slow-network timing (local loads finish
  in ~250 ms, which shrinks the late-listener window).
- Mobile still remounts the dashboard on each Dashboard tab visit (one clean
  load per visit now). Keeping it mounted (`v-show`) is a separate change.
