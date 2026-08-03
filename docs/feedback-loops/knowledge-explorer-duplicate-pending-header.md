# Feedback Loop — two stacked "Pending review" headers in the Knowledge Explorer

Reported: opening an instruction that has pending changes in `/agents` shows
**"Pending review" twice** — once in the detail pane's status bar (with the
version-history and Edit buttons) and again immediately below it, on the review
pane's own bar (with the change count and Accept all / Reject all).

## Root cause (validated)

Two independent headers that always render together, not a duplicated render:

1. `frontend/components/KnowledgeExplorer.vue` (pre-fix ~line 555) — the detail
   pane's generic `h-11` status bar. Its label is the instruction's status, and
   for a pending instruction that status *is* "Pending review"
   (`isPending(detail) ? $t('agentsPage.pendingReview') : getStatusLabel(...)`).
2. `frontend/components/instructions/InstructionTrackedChanges.vue` (pre-fix
   line 10) — the review component's own header bar, which it renders
   unconditionally.

They coincide by construction: the outer label appears when `isPending(detail)`
(`pendingInstrIds` membership) and the review pane renders when `reviewMode`
(`pendingBuilds.length > 0 && !reviewEmpty`) — effectively the same condition,
so *every* pending instruction opened in the explorer got both bars.

The review component owns a header because in its four other call sites it is
the only header — `report/ReportAgentPanel.vue`, `KnowledgeGroup.vue`,
`prompt/PendingInstructionItem.vue`, `tools/EditInstructionTool.vue`. The
Knowledge Explorer is the one host that already has a status bar of its own, and
there was no way to suppress the component's (props were `instructionId`,
`canApprove`, `compact`, `collapseContext`).

Secondary: the review header's strings ("Pending review", "N changes", "Accept
all", "Reject all", "Expand all") were hardcoded English while the outer bar was
localized — so in a non-English locale the two stacked bars disagreed.

## Fix

- `InstructionTrackedChanges` gains a **`hide-header`** prop and emits
  **`state({ total, busy })`**; `resolveAll` is exposed alongside `reload`. A
  host that already has a header can render the count and the bulk actions
  itself and delegate them back. Its own header is unchanged for the other four
  call sites.
- `KnowledgeExplorer` passes `hide-header`, mirrors `state` into `reviewHunks`,
  and renders `· N changes` next to the status label plus Reject all / Accept
  all before the Edit button — same styling as before, one bar instead of two.
- The review header's strings now come from `agentsPage.*`; new keys
  `agentsPage.collapse` / `agentsPage.expandAll` added to all 10 locales.

## Loop — deterministic reproduction (no external services)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
BOW_DATABASE_URL=sqlite:///db/agent.db uv run python scripts/seed_instruction_states.py
```

Dismiss onboarding (`PUT /api/organization/onboarding {"dismissed": true,
"completed": true}`), sign in as `admin@example.com` / `Password123!`, open
`/agents` → Sales Agent → Instructions, and click a pending row ("Discount
policy" = active+pending, "Legacy pricing rules" = inactive+pending).

**Observed PASS (post-fix):** one header —
`● Pending review · 1 change … Reject all | Accept all | 🕐 | Edit` — with the
tracked-changes text below it. Accept all and Reject all still resolve
server-side from their new position: the instruction text picks up the accepted
hunk (`GET /instructions/{id}` and `/review-hunks` confirm `suggestions: 0`), the
top-of-page chip drops 2 pending → 1 pending, and after a reload the resolved
rows carry no pending chip and no bulk buttons.

Evidence: `/tmp/bow-agent/ke-header{,2,3}/*.png` (headless Chromium capture).
`yarn build` (nuxt) passes.
