# /agents — stop loading ALL instructions on mount (design, not implemented)

## Problem

On mount, `KnowledgeExplorer.vue` calls `fetchAll()` →
`GET /api/instructions?skip=0&limit=200&include_own&include_drafts&include_archived`
and stores the full result in `allInstructions` (line 1860). **Everything** in
the tree is then derived client-side from that one flat list:

- counts: `globalCount`, `skillCount`, `pendingCount`, `agentCount(id)`,
  `agentPending(id)` (lines 1956–1960)
- rows: `listFor('global'|'skills')`, `listForAgent(id)`, `listForTable(id,tbl)`
  (lines 1973–1982)
- client-side **search + filters** via `applyFilters` (status/load/source/
  category/text) (line 1963)
- optimistic create/edit/delete patching of `allInstructions`
  (lines 1233, 1994, 2040, 2062, 2079, 2278, …)

So the page pays for hydrating up to 200 full instruction rows (text,
structured_data, data_sources, labels, references, build status) just to draw a
tree whose visible content is mostly counts + a few expanded rows.

## Idea (confirmed viable)

Load only what the initial render needs, lazy-load the rest:

1. **Global + Skills rows on mount.** These are the two always-present top-level
   groups; they're org-wide and typically few. One small list call each
   (`data_source_ids` empty / `kind=skill`).
2. **A counts endpoint** for everything the tree shows as a number/dot without
   rows: per-agent count, per-agent pending dot, global count, skill count, and
   total pending ("N pending" badge). One aggregate query, no row hydration.
3. **Lazy per-agent rows on expand.** When an agent node (or a table under it)
   is expanded, fetch `GET /instructions?data_source_ids=<agent>&include_global=false`
   and cache it. That single fetch yields BOTH the agent-level rows
   (`listForAgent`) and the table-referenced rows (`listForTable`) for that
   agent — they're all that agent's instructions.

The list endpoint already supports `data_source_ids`, `kind`, `include_global`,
and `search` filters, so the lazy fetches and the server-side search path need no
new list params.

## Backend scope

- **New `GET /api/instructions/counts`** (the only new endpoint). Returns, for the
  caller's visible scope:
  ```json
  { "global": N, "skills": N, "pending_total": N,
    "by_agent": { "<ds_id>": N, ... },
    "pending_by_agent": { "<ds_id>": true, ... } }
  ```
  Implementation: a `GROUP BY` over main-build instructions joined to
  `instruction_data_source_association`, applying the **same per-data-source
  visibility filter as `_execute_instructions_query`** (members + public DSes;
  global = no DS). Pending reuses the now-batched
  `get_pending_change_instruction_ids`, mapped to agents via the association
  (one extra query). Cheap and row-free.
- No change needed to `GET /instructions` for the lazy/search fetches.

## Frontend scope (the larger part)

Replace the single `allInstructions` store with:
- `globalRows`, `skillRows` — loaded on mount.
- `agentRows: Map<agentId, Instruction[]>` — loaded on expand, cached.
- `counts` — from the counts endpoint (drives all badges/dots).

Rework:
- `globalCount/skillCount/pendingCount/agentCount/agentPending` → read from
  `counts` (not from rows).
- `listFor('global'|'skills')` → from `globalRows`/`skillRows`.
- `listForAgent/listForTable` → from `agentRows[id]` (trigger load on expand;
  show a Spinner until loaded — pattern already exists via `instrLoading`).
- **Search / filters** (`applyFilters`): lazy loading breaks client-side global
  search. Route an active search/filter to a server query
  (`GET /instructions?search=…&status=…&…`) and render those results in a flat
  "search results" view, instead of filtering the in-memory list. (The endpoint
  already supports all these filters.)
- **Optimistic updates** (create/edit/delete/approve/discard): update the right
  cache (global vs the agent's `agentRows[id]`) AND adjust `counts` so badges
  stay correct without a full refetch. This is the fiddliest part and the main
  source of risk.

## Tradeoffs / risks

- ✅ Mount stops hydrating up to 200 full rows; initial payload becomes
  counts + a few global/skill rows. Per-agent rows load only when looked at.
- ⚠️ More total requests (counts + one per expanded agent), but each is small and
  most agents are never expanded.
- ⚠️ Counts vs lazily-loaded rows can drift after mutations — must keep `counts`
  in sync on every optimistic update (or refetch counts after mutations).
- ⚠️ The counts endpoint MUST mirror the list's visibility filter exactly, or
  agent badges will show counts the user can't drill into.
- ⚠️ Frontend refactor touches the optimistic-update paths and search — medium
  effort, the riskiest piece.

## Answer to "only global + counts + pending count?"

Almost — with two refinements:
- The Global (and Skills) group needs the actual **rows**, not just a count, to
  render its rules — so load those rows on mount (small).
- "Counts" should be one endpoint covering per-agent count **and** per-agent
  pending dot **and** global/skill/total-pending, so the tree draws fully without
  any per-agent row fetch.
- Then per-agent rows lazy-load on expand. Search/filter needs a server-side path.

## Open questions for review
1. Load Skills rows on mount too, or lazy-load Skills on expand like agents?
2. Search behavior when lazy: server-side flat results view — acceptable UX?
3. Keep counts fresh via per-mutation adjustment, or just refetch the counts
   endpoint after each mutation (simpler, one cheap call)?
