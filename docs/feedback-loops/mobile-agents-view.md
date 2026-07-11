# Feedback Loop — "Mobile view of /agents looks really bad"

Reported from a phone (iPhone, Hebrew/RTL locale, `bow.fattal.co.il`): on the
`/agents` page the page title and subtitle are crushed into a one-word-per-line
sliver next to the header buttons, and in the agent-overview header the name,
"Public"/"Training" pills, sparkline and "New report" button overlap each
other. The claim validated here: the breakage is a mobile-width layout bug in
`KnowledgeExplorer.vue` (not RTL-specific — LTR reproduces identically).

## Root cause (validated)

All in `frontend/components/KnowledgeExplorer.vue`:

- **Page header** (template line ~4): `flex items-center justify-between` with
  no `flex-wrap`; the actions cluster (pending badge, `GitConnectionButton`,
  "New") can't shrink, so on a 390px viewport the title/subtitle column is
  squeezed to ~175px — one word per line — and the buttons' own labels break
  onto two lines.
- **Agent overview header** (line ~285): same pattern, worse. The title row
  (`flex items-center gap-2 min-w-0`) holds icon + status dot + name +
  public-pill + `PublishStatusControl` + auth badges, most `shrink-0`; with no
  wrap the pills overflow the `min-w-0 flex-1` column and paint over the
  adjacent actions cluster (`shrink-0`: sparkline + Self Learning + New report
  + close). The description gets the leftover ~70px → one word per line.
- Fixed `100vh` root height (line 2) ignores mobile browser chrome (the
  connections footer lands under the iOS toolbar), and the version-history
  pane is a fixed `w-72` third column even on phones.

RTL was structurally fine (the codebase already uses logical `ps/pe/ms/me`
utilities) — the same squeeze just mirrored.

## Loop A — deterministic reproduction (no external services)

```bash
tools/agent/boot_stack.sh                     # backend :8000 + frontend :3000 (prod build)
cd backend && uv run python ../tools/agent/seed_org.py --demo   # admin@example.com / Password123! + Music Store agent
export PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
```

Reproduction is the Playwright spec (390×844 viewport, logged-in admin):

```bash
cd frontend
npx playwright test tests/instructions/agents-mobile-viewport.spec.ts
```

Observed FAIL on pre-fix code (commit `fbb95bd`):

```
Error: expect(received).toBeGreaterThan(expected)
Expected: > 300
Received:   176.1875        # subtitle crushed beside the header buttons
  1 failed
```

Screenshot evidence (same loop, both locales) is committed under
`media/pr/mobile-agents-view-styling/before-{en,he}-{tree,detail}.png` — the
`before-*-detail` shots show the pills/buttons overlap exactly as in the
report.

## The fix

`frontend/components/KnowledgeExplorer.vue`:

- Page header and agent-overview header rows get `flex-wrap` with a
  `grow basis-60/64` title block (`grow` + `basis-*`, not `flex-1`, because
  Tailwind emits `.flex-1`'s `flex-basis: 0%` after `.basis-*` and it would
  win): on phones the actions drop to their own row instead of crushing the
  title; on ≥ tablet widths nothing changes (single row, actions at the end
  via `ms-auto`).
- The agent-overview title row wraps its pills below the name
  (`flex-wrap gap-y-1.5`), and action buttons get `whitespace-nowrap`.
- Root height uses `100dvh` with a `100vh` fallback (`.ke-viewport`,
  `--ke-banner` for the top banner) so the footer clears mobile browser chrome.
- Version-history pane overlays the detail pane on mobile
  (`absolute inset-0 z-20`) instead of forcing a `w-72` third column.
- Detail-pane `px-8`/`px-6` paddings become `px-4 sm:px-*`.

`frontend/components/instructions/GitConnectionButton.vue`: `whitespace-nowrap`
so "Connect Git" can't break into two lines.

Re-run of the same loop after the fix:

```
Running 1 test using 1 worker
  1 passed (15.2s)
```

After screenshots: `media/pr/mobile-agents-view-styling/after-{en,he}-{tree,detail,instruction}.png`.
Desktop (1440×900) re-checked in both locales — layout unchanged.

## What this proves / regression notes

- The crushed-header and overlapping-pills breakage reproduces at phone width
  in **both** LTR and RTL, and is fixed by wrap-aware headers; the spec pins
  the invariants (no horizontal overflow, subtitle keeps a readable column,
  "New" button inside the viewport) rather than one magic string.
- The spec runs in the existing `features` Playwright project (auth via the
  suite's `admin.json` storage state) — nothing external is required.
