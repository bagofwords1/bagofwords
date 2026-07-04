# Sandbox Feedback Loop — /agents connections footer: "View all" overflows the tree pane

Reproduces the reported bug: on **`/agents`**, once an org has enough
connections, the connections footer at the **bottom of the tree pane** overflows
horizontally and the **"View all" link spills past the pane's right edge** into
the detail pane.

This doc is the runnable feedback loop. **Status: reproduced and fixed** (option
3 below). See "The fix" for the applied change and after-results.

---

## The symptom (reproduced)

At the default tree-pane width (**300px**), the footer row already overflows with
**4 connections**, and overflows harder once the **`+N` chip** appears at
**5+ connections**. "View all" (the last child, pushed by `ms-auto`) ends up
outside the pane:

![300px pane, 8 connections — View all spilled into the detail pane](sandbox-repro/agents-connections-overflow/repro-w300-n8.png)

Contrast — **3 connections** at the same 300px width fits fine:

![300px pane, 3 connections — fits](sandbox-repro/agents-connections-overflow/ok-w300-n3.png)

---

## Where it lives

`frontend/components/KnowledgeExplorer.vue:219-242` — rendered by
`frontend/pages/agents/index.vue` via `<KnowledgeExplorer />`.

```
<!-- Connections footer -->
<div class="border-t ... px-3 py-2 flex items-center gap-2">   <!-- ← no flex-wrap, no min-w-0, no overflow -->
  <span ... me-1>Connections</span>                            <!-- label, single word, won't shrink -->
  <UTooltip v-for="c in connections.slice(0, 4)">…icon…</UTooltip>   <!-- up to 4 fixed w-6 buttons -->
  <UTooltip v-if="connections.length > 4">+{{ n-4 }}</UTooltip>      <!-- the +N chip (5+) -->
  <UTooltip v-if="…">…new (+)…</UTooltip>
  <button class="ms-auto …">View all</button>                  <!-- pushed to the end -->
</div>
```

The parent tree pane is fixed/resizable:
`aside … :style="{ width: treeWidth + 'px' }"`, `treeWidth = ref(300)`,
`clampTreeWidth = min(600, max(220, w))` (`KnowledgeExplorer.vue:39, 1344-1345`).

### Root cause

The footer is a single-line flex row (`flex items-center gap-2`) with **no
`flex-wrap`, no `min-w-0`, and no overflow handling**. Its children have a fixed
intrinsic width that does **not** shrink to fit:

- the **"Connections"** label is one word → can't wrap/shrink;
- each connection icon is a **`w-6` button wrapped in `<UTooltip>`**
  (Nuxt UI v2 renders a `relative inline-flex` wrapper), so it holds ~24px;
- the `+N` chip and the dashed "new" button add more fixed width.

Summed against the **300px** default pane (minus `px-3` padding), the row's
content is wider than the pane. Because there's no wrap/overflow rule, the
overflow simply extends past the pane's right border, and `ms-auto` (which needs
free space to push "View all" right) collapses — so **"View all" lands outside
the pane**, over the detail section. Widening the pane past the content width
(~460px) clears it, confirming it's purely a width-vs-content layout problem.

Note the icon count is capped at 4 (`connections.slice(0, 4)`), so the overflow
is a **fixed step**, not proportional to connection count — every org with ≥4
connections at the default width sees it, and it looks identical at 5, 8, or 12.

---

## The loop

Isolated, offline harness under `sandbox-repro/agents-connections-overflow/`
(the footer markup copied verbatim, `<UTooltip>` modelled as its
`relative inline-flex` wrapper, driven by the real Tailwind engine). `?w=` = tree
width, `?n=` = `connections.length`.

```bash
cd sandbox-repro/agents-connections-overflow
npm install playwright                          # browsers pre-installed in sandbox
curl -sL https://cdn.tailwindcss.com -o tailwind.js   # headless browser has no proxy; load locally
CHROME_BIN=/opt/pw-browsers/chromium-1194/chrome-linux/chrome node shot.mjs
```

`shot.mjs` measures the footer's horizontal overflow and how far "View all"'s
right edge lands past the pane's right edge (`viewAllSpill`, +ve = outside).

### Observed

| pane width | connections | footer overflow | View all spill past pane edge | result |
|---|---|---|---|---|
| 300 (default) | 3 | 0 px | −10 px (inside) | ✅ fits |
| 300 (default) | **4** | **23 px** | **+22 px (outside)** | ❌ overflow begins |
| 300 (default) | **5** (first `+N`) | **61 px** | **+60 px** | ❌ |
| 300 (default) | 8 | 61 px | +60 px | ❌ (same as 5) |
| 300 (default) | 12 | 61 px | +60 px | ❌ (same as 5) |
| **220 (min)** | 8 | **141 px** | **+140 px** | ❌ worst |
| 460 (wide) | 8 | 0 px | −13 px (inside) | ✅ fits |

**Threshold:** overflow starts at **4 connections** at the default 300px width and
is constant from 5 up (icons capped at 4 + one `+N` chip). Narrower pane → worse;
wide pane → gone.

---

## Fidelity / caveats

- This is an **isolated-markup** repro, not the full Nuxt app: it reproduces the
  exact footer subtree with the real Tailwind engine and the Nuxt UI `UTooltip`
  wrapper, but does not boot the backend/auth/seed path.
- It assumes the project's `tailwind.config` does not override the core spacing
  scale for the handful of utilities involved (`gap-2`, `w-6`, `px-3`, `h-6`,
  `px-1.5`, `text-[11px]`, `me-1`) — standard values the project uses unchanged.
- Exact pixel spill will vary slightly with the real DataSourceIcon assets and
  font metrics, but the **overflow direction and threshold** are structural.

---

## Candidate fixes (considered)

Options, roughly increasing effort:

1. **Let the row wrap** — add `flex-wrap`. Cheapest; changes footer height.
2. **Scroll the whole strip** — `overflow-x-auto`. A scrollbar in a ~24px strip
   is ugly and can still scroll "View all" out of view.
3. **Pin the functional items; clip only the icon preview** — the icon cluster is
   the sacrificial part. ✅ chosen.
4. **Responsive `slice`** — reduce visible icon count via JS width tracking.
   Most code for a pure-CSS problem.

## The fix (applied — option 3)

`frontend/components/KnowledgeExplorer.vue:219-248`. The icon preview is the only
functionally-redundant part (the modal shows the full list; `+N` already
summarizes the overflow), so it becomes the only flexible/clipped element, and
the affordances that *do* something stay pinned:

- **Icon preview** wrapped in `flex items-center gap-2 min-w-0 shrink-[9999]
  overflow-hidden py-1 -my-1 pe-1 -me-1`. `min-w-0 + overflow-hidden` lets it
  clip; `shrink-[9999]` gives it top shrink priority so it collapses *before*
  anything else; the `py/-my + pe/-me` padding+negative-margin pair gives the
  absolutely-positioned status dots room so `overflow-hidden` doesn't clip them.
- **`+N` chip, "new" button, "View all"** → `shrink-0` (never shrink; always
  visible).
- **Label** → `min-w-0 truncate` (no longer `shrink-0`). It yields *last* — only
  once the icon preview is fully collapsed (thanks to the icon strip's
  `shrink-[9999]`) does the label truncate, so at the 220px hard-minimum it
  degrades to `CONNE…` while "View all" stays fully in view. At the default
  300px width the label is never truncated.

### After-results (same harness, `?fix=1`)

| pane width | connections | footer overflow | View all spill | before → after |
|---|---|---|---|---|
| 300 (default) | 8 | **0 px** | −13 px (inside) | +60 px → **fixed** |
| 300 (default) | 3 / 12 | 0 px | −13 px (inside) | fixed |
| **220 (min)** | 8 | **0 px** | −13 px (inside) | +140 px → **fixed** |

Overflow is 0 across the whole 220–600px resize range. Icons are the only thing
that visually give way (they clip, then the label truncates at the extreme min);
`+N`, "new", and "View all" always stay in the pane.

> Verified via the isolated harness (`?fix=1`), which mirrors the applied classes.
> Not booted through the full Nuxt app; the change is template-only (CSS utility
> classes, no script/binding changes).

## Artifacts

- `sandbox-repro/agents-connections-overflow/repro.html` — footer harness (`?fix=1` toggles the fix)
- `sandbox-repro/agents-connections-overflow/shot.mjs` — screenshot + measurement driver
- `sandbox-repro/agents-connections-overflow/repro-*.png` — before (bug)
- `sandbox-repro/agents-connections-overflow/fixed-*.png` — after (fixed)
