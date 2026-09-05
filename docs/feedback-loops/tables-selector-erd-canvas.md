# Tables selector ERD — implementation and local feedback loop

Adds a minimal **Table / ERD** toggle to the existing selector. Both views use
one draft selection and the existing Save/Save & Continue operation. Selecting a
table reveals its direct declared or suggested neighbors without selecting them. The diagram
supports focus, one-hop expansion, search, key/relationship details, and usage on each card with last-used and answer-feedback details on hover.

## Working context

- Branch: `codex/tables-erd`.
- Base: `b161464e`, the user's current PR head (`claude/umbrella-pr-recent-uifop4`).
- Separate worktree: `/private/tmp/bow-tables-erd`.
- Local frontend: `http://127.0.0.1:3107`; backend: `http://127.0.0.1:8107`.
- Dedicated SQLite application DB: `/private/tmp/bow-tables-erd-run/app.db`.
- Main checkout and its running services are not used as the app under test.
  Existing installed dependencies are reused through a node_modules symlink and
  the main checkout's Python 3.12 venv. The local Node runtime is 22.22.3. Auth files stay outside the repository.

## Product contract

| State | Rendering | Agent activation |
| --- | --- | --- |
| Selected | Blue solid-border checked card | Enabled after Save |
| Shown, not selected | Gray dotted unchecked card on a muted background | Disabled |
| Not shown, not selected | Discoverable through search or Expand | Disabled |

- Active tables always bring their immediate incoming and outgoing neighbors
  into the graph model. Already selected neighbors stay checked.
- A revealed ghost does not recursively reveal its own neighbors. Selecting it
  or explicitly expanding it reveals the next hop.
- Focus, search preview, expand, drag, and metrics do not activate tables.
- Deselecting retains the card in the current exploration, including changes
  made in Table view. Reset clears exploration-only ghosts; Clear focus only
  changes the highlight and viewport.
- Discovery uses the same filters as Table view. Context nodes outside filters
  remain visible; their details identify this condition.
- The catalog loads across API pages, independently of list pagination. Rendering
  includes every selected/revealed node. Vue Flow mounts only viewport-visible
  cards; connected neighborhoods are laid out independently and packed into rows.
  Search focuses a table without truncating the rest of the graph.
- A missing neighbor is a potential gap, never a validation error or Save blocker.
  Unresolved relationship targets are described without exposing their names.

## Implementation map

| File | Responsibility |
| --- | --- |
| `frontend/components/datasources/TablesSelector.vue` | Toggle, complete permission-scoped catalog, shared filters/draft and connection scopes, one delta Save |
| `frontend/components/datasources/TablesCanvas.vue` | Vue Flow viewport, focus/expand, viewport rendering, selection menu, packed layout, detail panel |
| `frontend/components/datasources/TableCanvasNode.vue` | Checked/ghost table card, query marker, checkbox and permitted editor action |
| `frontend/components/datasources/TableMetrics.vue` | Usage on each card; successful/failed queries, feedback, last use and cache status on hover |
| `frontend/utils/tableGraph.ts` | Connection/schema-scoped relationship resolution, one-hop visibility, shared filter predicates |
| `frontend/components/datasources/{CatalogSelector,AgentKnowledgeTabs}.vue` | One combined onboarding selector across table connections; preserve the step when Save fails |
| `backend/app/schemas/datasource_table_schema.py` | UTC last-used/cache timestamps and safe custom-query display metadata |
| `backend/app/services/data_source_service.py` | Populate stats and safe query metadata; stable ID tie-breaker for pagination |
| `locales/{en,es,he,fr,sv,ar,ru,de,pt,it}.json` | Matching translated ERD namespace |
| `frontend/nuxt.config.ts` | Optional `BOW_API_TARGET` override for an isolated local backend |

Vue Flow was already installed and registered. Dagre's existing `dagre-d3-es`
package is now declared directly at the already-locked version, 7.0.14, and only
its layout/graph modules are imported. The ERD component loads asynchronously.
Initial layout uses fixed 280×180 card geometry; explicit Arrange recomputes it.
Offscreen coordinates stay synchronized with Vue Flow’s viewport index. Newly
revealed nodes preserve existing card positions, and Expand brings them into view. Self-links route around the card.

Bulk selection now resolves the complete matching catalog into the same draft
map as individual checkboxes. Save sends the final delta once, so an individual
override works even for a table never visited in the list. Late list responses cannot overwrite the complete catalog. Custom-query updates
and completed catalog syncs invalidate cached graph data while preserving dirty
choices and refreshing clean rows. A failed delta keeps
the draft and returns failure to the parent instead of navigating onward.

## Tasks

- [x] Create a separate branch/worktree from the current PR head.
- [x] Inspect selector embeds, selection/save behavior, relationships and stats.
- [x] Seed a local synthetic source and capture the baseline UI before integration.
- [x] Add the Table / ERD toggle using the existing theme and controls.
- [x] Implement three states, direct neighbors, focus, expansion, discovery,
  stable positions, details, empty/retry states and large graphs.
- [x] Share selection, filters, counts and Save across views and parent embeds.
- [x] Add usage/last-used/feedback metrics and all ten locale catalogs.
- [x] Add a deterministic fixture, focused Playwright suite and graph unit checks.
- [x] Run existing backend pagination, selection and reader-permission regressions.
- [x] Run the existing 30-case locale sweep and check catalog drift.
- [x] Complete the baseline-to-feature rerun and inspect before/after screenshots.
- [x] Complete the initial production build.
- [x] Refine selected/ghost strokes, reuse USelectMenu, and show connection icons.
- [x] Add labelled key-name suggestions and combined multi-connection onboarding.
- [x] Replace the node cap with viewport rendering and packed large-graph layout.
- [x] Finish refinement acceptance and production build.
- [x] Replace the pill toggle with text tabs in the Reload row and shorten the disabled custom-query hint.
- [x] Show the draft selected count in the picker and use gray checkbox-only ghost cards.
- [x] Replace overlays with per-card usage and a complete metrics tooltip.
- [x] Unify cached custom queries with table selection, preserving editor permissions.
- [x] Complete final cleanup acceptance, screenshots and production build.

## Fixture and isolation

`tools/agent/seed_org.py --sqlite-sources 1` creates the synthetic org/source
through the real API. `tools/agent/seed_tables_canvas.py` extends that fixture:

- `orders → customers`; `line_items → orders` and `line_items → products`;
  `payments → orders`; `employees → employees`.
- Only `orders` starts selected. Products therefore starts hidden, while
  customers, line_items, payments, and companies start as unchecked neighbors.
- 520 archive tables plus eight source tables across two connections force catalog loading beyond the
  API's 500-row page size. The list's page size remains 100.
- A synthetic historical rollup has known usage, feedback and last-used values;
  other tables have no rollup, so unknown data can be distinguished from zero.
- `orders.company_id → Reference.companies.id` has no FK declaration and
  exercises a suggested relationship across two real SQLite connections.
- A real member account can view the public test agent but cannot configure it.

The SQLite connector currently emits no foreign-key metadata, and the public
selection API cannot write relationships or historical statistics. For those
otherwise unreachable fixture states only, the seed script writes FK metadata
and rollups into the dedicated test application DB. Schema reads, authorization,
selection persistence and onboarding navigation all use the real app. No live
LLM, external database, customer data or external credentials are needed.

## Reproduce locally

Use a new dedicated DB/run directory. Check ports before starting; do not stop
another checkout's server. Commands below document this run's machine paths.
Adapt the Python executable/browser path to the available local environment.

```bash
cd /private/tmp/bow-tables-erd/backend
export TESTING=true
export ENVIRONMENT=production
export TEST_DATABASE_URL=sqlite:////private/tmp/bow-tables-erd-run/app.db
export MPLCONFIGDIR=/private/tmp/bow-tables-erd-run/mpl
mkdir -p /private/tmp/bow-tables-erd-run
/Users/yochze/Desktop/bagofwords/backend/.venv/bin/python -m alembic upgrade head
/Users/yochze/Desktop/bagofwords/backend/.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8107
```

In another terminal:

```bash
cd /private/tmp/bow-tables-erd/frontend
BOW_API_TARGET=http://127.0.0.1:8107 node node_modules/nuxt/bin/nuxt.mjs dev --host 127.0.0.1 --port 3107
```

Seed with stdout redirected because the existing org helper prints auth tokens:

```bash
cd /private/tmp/bow-tables-erd
/Users/yochze/Desktop/bagofwords/backend/.venv/bin/python tools/agent/seed_org.py --base-url http://127.0.0.1:8107 --org-name 'ERD verification' --email erd@example.com --sqlite-sources 1 --db-path /private/tmp/bow-tables-erd-run/app.db > /private/tmp/bow-tables-erd-run/seed.json
/Users/yochze/Desktop/bagofwords/backend/.venv/bin/python tools/agent/seed_tables_canvas.py --seed /private/tmp/bow-tables-erd-run/seed.json --app-db /private/tmp/bow-tables-erd-run/app.db --extra-tables 520
```

The ERD seed refreshes auth after a backend restart, creates/reuses the reader,
sets known activation, and restores onboarding to an unfinished state. Each
Playwright case resets activation and onboarding independently as well.

```bash
cd /private/tmp/bow-tables-erd/frontend
export BOW_ERD_SEED=/private/tmp/bow-tables-erd-run/seed.json
export PLAYWRIGHT_CHROMIUM_EXECUTABLE='/Users/yochze/Library/Caches/ms-playwright/chromium-1193/chrome-mac/Chromium.app/Contents/MacOS/Chromium'
node node_modules/@playwright/test/cli.js test --config playwright.tables-canvas.config.ts --reporter=line
node --experimental-strip-types tests/unit/tableGraph.mjs
PLAYWRIGHT_BASE_URL=http://127.0.0.1:3107 node node_modules/@playwright/test/cli.js test --config playwright.i18n.config.ts --reporter=line
BOW_API_TARGET=http://127.0.0.1:8107 NODE_OPTIONS=--max-old-space-size=6144 node node_modules/nuxt/bin/nuxt.mjs build
```

Backend regression command (from `backend/`):

```bash
TESTING=true ENVIRONMENT=production MPLCONFIGDIR=/private/tmp/bow-tables-erd-run/mpl \
  /Users/yochze/Desktop/bagofwords/backend/.venv/bin/python -m pytest \
  tests/e2e/test_data_source.py::test_paginated_full_schema \
  tests/e2e/test_data_source.py::test_bulk_update_and_delta_update \
  tests/e2e/rbac/test_rbac_data_sources.py::test_full_schema_hides_inactive_tables_from_non_managers \
  tests/unit/test_custom_query_permissions.py \
  --db=sqlite -q
```

## Loop A — discriminating baseline and pass

Run the same `three states` test with the base selector before integrating the
feature. Preconditions assert the real authenticated selector has loaded.
`BOW_ERD_BASELINE=1` records a baseline screenshot. The first feature assertion
requires the ERD toggle; it must fail on the base for that reason, not for an
unavailable server or failed login. Restore the feature and rerun the same spec.

The live suite covers:

1. Selected/ghost/hidden states, off-page relationship targets, incoming and
   outgoing focus details, expansion without activation, add, view switching,
   shared search, Save/reload, and deselection.
2. Onboarding, a 520-table bulk selection with an individual override,
   search-to-node navigation, usage/last-used/feedback values, Hebrew and dark mode.
3. Simulated HTTP 503 on Save: the real draft remains, no persistence occurs,
   onboarding does not advance, and retry succeeds after the simulated fault ends.
4. Simulated catalog-load failure/retry, empty selection, keyboard activation,
   and a self relationship.
5. Real reader permissions: only the selected table is served/rendered, with no
   activation controls, ghost metadata or Save action.
6. A filtered list response intentionally arrives after the complete catalog.
   It cannot replace the local draft or hide a locally deselected matching table.
   Discovery also opens correctly when filters were set before entering ERD.

The browser suite lives under `frontend/tests/tables_canvas/`, outside the main
Playwright projects, so its explicit local seed requirement cannot break regular
CI discovery.

Graph unit checks also cover connection/schema collisions, grouped join columns,
self-links, unresolved targets, one-hop visibility, filter predicates, suggested
links, ambiguous targets, incompatible types, local-source preference, and a
1,000-table metadata catalog.

## Loop B — visual evidence

Evidence is under `media/pr/tables-erd/`. Paired desktop screenshots use the same
1440×900 viewport, synthetic fixture and English locale. Hebrew/dark and narrow
screenshots supplement them. The flow recording comes from the interacting
Playwright acceptance test, not a passive page recording.

| Evidence | File |
| --- | --- |
| Agent before / after | `before-agent-list-en.png`, `after-agent-canvas-en.png` |
| Onboarding before / after | `before-onboarding-list-en.png`, `after-onboarding-canvas-en.png` |
| Large graph | `after-large.png` |
| Metrics hover | `after-metrics-hover.png` |
| Cached custom query | `after-custom-query.png`, `after-custom-query-metrics.png` |
| Cleanup before | `before-cleanup-en.png`, `before-cleanup-he-dark.png`, `before-cleanup-onboarding.png` |
| Hebrew / dark before / after | `before-agent-list-he-dark.png`, `after-agent-canvas-he-dark.png` |
| Narrow viewport | `after-narrow.png` |
| Empty state | `after-empty.png` |
| Reader | `after-reader.png` |
| Focus, expand, add, switch, Save | `focus-expand-add-save.gif` |

## Execution record

| Check | Observed result |
| --- | --- |
| Initial base selector inspection | ERD button count 0; agent/onboarding screenshots captured |
| Graph invariants | PASS |
| Backend pagination, delta/bulk and reader scope | 3 passed (SQLite); existing dependency/deprecation/async-cleanup warnings recorded |
| Existing locale sweep | 30 passed again after refinement (30.9 seconds) |
| Catalog key drift | No increased missing/extra keys in any locale |
| Discriminating baseline | Expected FAIL: the loaded base selector has no ERD toggle |
| Initial browser acceptance | 6 passed against the isolated 527-table fixture |
| Refinement acceptance | 9 passed in 59.1 seconds; 528-table fixture across two real local connections |
| Visual inspection | Loaded before/after agent and onboarding views, Hebrew/dark, narrow, large, empty, reader and overlays inspected; interaction GIF recorded |
| Last-used API serialization | Known fixture timestamp returned as `2026-09-01T10:00:00Z` |
| Initial production build | PASS (exit 0); existing duplicate-key/import and chunk-size warnings remain |
| Refinement production build | PASS (exit 0), client + server + Nitro output; existing warnings unchanged |
| Cleanup browser acceptance | 11 passed on development; all 11 passed again on the production build (41.0 seconds), using 528 tables across two connections plus a temporary custom query |
| Cleanup production build | PASS (exit 0); client, server and Nitro output built successfully; existing warnings unchanged |
| Cleanup backend regressions | 14 passed: pagination, delta/bulk, reader schema scope and custom-query permissions |
| Cleanup locale sweep | 30 passed (26.8 seconds); ten ERD catalogs match with no additional global key drift |

## Limits of this evidence

A passing loop proves shared activation, navigable known relationships beyond
pagination, explicit exploration, existing permission boundaries, and usability
in the captured local conditions. It does not prove every real source supplies
complete relationship metadata, every omitted neighbor is necessary, or answer
quality improves. Solid edges represent connector-reported references. Dashed edges are explicitly
labelled unverified suggestions: a named key such as `company_id` points at a
unique compatible identity column on `companies`. Generic shared `id` columns,
ambiguous targets, composite keys, incompatible types, and declared source
columns do not generate guesses. Local connection/schema matches take
precedence; a unique match may span connections. These suggestions do not
validate data or imply that the connections support a federated SQL join. Feedback counts describe answers using
a table, not a quality score for the table. Statistics use the existing all-time
agent rollup; absent rollups are shown as unknown.

No new relationship storage/migration, external data inspection, automatic
neighbor activation, PR publication or deployment is part of this change.

## Refinement — selected styling, picker, connections and scale

User requested blue selected strokes, gray dotted unselected strokes, an existing
`USelectMenu` with selected tables first, connection icons and relationships
without explicit foreign keys, and support for hundreds of tables.

The baseline browser check failed on the original gray selected border
(`rgb(209, 213, 219)` instead of `rgb(59, 130, 246)`). The refined picker writes to
the same selection draft, orders selected options first, and uses the same
connection/schema/search constraints as the table view. Search previews remain
non-activating; choosing a menu option explicitly changes activation.

The filtered-picker test also reproduced a discovery panel covering menu
options; opening the picker now closes the preview and keeps it closed while
selection changes.

The second connection exposed the old per-connection onboarding selectors, which
prevented a combined graph. Onboarding now uses one selector scoped to all its
table connections. Source filtering narrows that scope in both views.

Additional browser cases cover picker ordering/search/persistence, real
cross-connection suggestions and icons, and shared connection filters in
onboarding. Viewport checks caught and fixed stale coordinates for offscreen
cards during relayout; these assertions now check all initial neighbors and both
ends of the large graph. The large-catalog case fits all 525 graph nodes from a 528-table
catalog, then focuses an off-page table and checks that fewer than 50 cards
remain mounted. The 520-table bulk selection still saves individual overrides.

Additional evidence: `before-refinement-en.png`,
`before-refinement-he-dark.png`, `after-selection-menu.png`, and
`after-cross-connection.png`. The standard after screenshots and interaction GIF
are refreshed for this version. Original list baselines predate the second
connection; refinement comparisons use the same viewport and synthetic source.

## Cleanup — metrics, compact toolbar and custom queries

Table / ERD are now understated text tabs beside Connections, aligned with
Reload. The feature-off hint reads “Custom queries off.” The canvas picker
shows the draft selected count, and unchecked neighbors have a darker gray
background with dotted borders and one checkbox. The overlay selector is gone.
Usage is always present when statistics are enabled; its tooltip shows success,
failure, feedback and last use.

Custom queries now use the ordinary catalog row in both views. Their checkbox
changes the same draft and only Save activates them for the agent. Creating or
editing the connection-level query still uses the existing editor; creating a
query from this selector no longer silently activates it. Query cards show a
bolt and output columns; cache status and last refresh appear in the tooltip.
Only connection administrators receive the pencil action. The schema endpoint
adds safe display metadata, never SQL, artifact paths/keys or RLS policy details.

Relationships use the output schema and the same declared/suggested resolver.
SQL source-table dependencies are not inferred: a materialized aggregate over
orders can be selected alone, because the agent reads its cached result.

The cleanup baseline test failed on the old picker label instead of the
required selected count. Live screenshot inspection then caught tooltip rows
clipped by the UI library’s default fixed height. The tooltip now grows to fit
its content, and the acceptance test checks the last row is inside its bounds.

The new custom-query browser case creates a real cached SQLite aggregate via
the API, checks safe metadata and initial inactivity, selects it in Table view,
switches to ERD, checks no source-table node is required, inspects cache metrics,
opens the permitted editor, saves, reloads, and switches to a real reader account
to verify that edit/activation controls are absent. It deletes the synthetic
query and restores the feature setting afterward.

## Laptop workspace — full screen, wide schemas and visible metrics

The user’s laptop screenshots showed small cards, a short canvas, a floating
panel over related tables, and Save obscured by the support widget on the right.
At baseline `e82600e2`, `TablesCanvas.vue:2` capped the height with
`clamp(380px, 52vh, 560px)`, its details panel overlaid the viewport, and
`TablesSelector.vue:552` aligned Save to the end of the row. The laptop test
captured `before-workspace-laptop.png`, then failed because the Full screen
button did not exist.

Changes:

- Cards retain three preview columns, prioritizing relationship keys. A
  Columns (N) control opens a searchable column list in the details dock.
  Only the first 50 matches render initially; Show more reveals another 50.
  The list scrolls within the dock, so hundreds of columns never grow a node.
- The canvas fills remaining viewport space, with room for Save. Initial
  zoom has a readable floor; explicit Fit still shows the entire graph.
  Details reserve space beside the graph, or below it on narrow screens.
- Full screen moves the same selector to a body-level overlay, retaining its
  draft, node positions and viewport. Search, both views, selection, query
  editing, Save and an explicit exit remain available. Escape exits, keyboard
  focus stays within the selector, and the app surface is restored on exit.
  Onboarding has Save within full screen, then its existing Save & Continue
  when returning to the embedded step.
- Save and the onboarding continuation buttons stay on the physical left in
  both English and Hebrew, clear of the support widget.
- The card footer reuses the table list’s usage, successful/failed query and
  positive/negative feedback counts and icons. Centrality is shown when supplied
  by the existing API. Large counts use compact formatting; the hover details
  retain full values, last use and cache status. No popularity score is invented.

The `laptop canvas, full screen, wide columns and visible metrics` case runs at
1366×768. It overrides only the synthetic orders response’s column metadata to
300 columns and a known centrality value, leaving authentication, selection and
saving on the real local API. It checks the left-aligned Save position, full-screen
height, the 50-row initial bound, searching the last column, visible metrics,
view switching, Escape, and preserved activation. The custom-query regression
also opens the real editor from full screen and exits afterward.

Re-run with the existing commands above, or isolate the new checks with:

```bash
node node_modules/@playwright/test/cli.js test --config playwright.tables-canvas.config.ts --grep 'laptop canvas|cached custom'
```

Evidence: `before-workspace-laptop.png`, `after-workspace-laptop.png`,
`after-workspace-fullscreen.png`; the standard English/Hebrew/onboarding and
large-graph screenshots are refreshed. The new flow is `fullscreen-columns.gif`.

Verification: all 12 ERD browser cases passed on the development stack, including
528 objects across two connections. The additional full-screen/editor and view
switching checks passed. Graph invariants passed, and all ten ERD catalogs have
matching keys with no increased global drift. Final production verification: 12 browser cases passed in 43.2 seconds,
including physical-left Save in Hebrew, full-screen Save persistence, keyboard
navigation, and the graph/dock boundary. The production build passed (exit 0).
All 30 locale checks passed on production without retries (5.2 seconds); the
concurrent development run had one delayed Spanish page that passed on retry.

Screenshot review caught two final layout details: RTL must preserve the physical
left for Save, and Vue Flow’s default `width: 100%` must be overridden with
`width: auto` for the reserved dock inset to take effect. The browser suite now
asserts both boundaries and that the cross-connection neighbor remains visible.

### View labels — List / Visual

Renamed the two view labels to **List** and **Visual**, with matching translations in all ten catalogs. Internal view identifiers and selection behavior remain unchanged. Updated browser locators, including Hebrew.

Verification: 2 targeted canvas browser tests passed (English toolbar, onboarding, selection, Hebrew/dark mode); all 30 locale checks passed. Catalog key structures are unchanged. Evidence: `media/pr/tables-erd/before-view-labels.png`, `after-metrics-hover.png`, and `after-agent-canvas-he-dark.png`.

### Selection dropdown cleanup

Removed the redundant Selected / Not selected text from dropdown options. The native checkmark and `aria-selected` state remain. The targeted browser regression passed: selected-first ordering, checked/unchecked accessibility states, search, selection, and Save. Before/after evidence: `before-selection-menu-labels.png` and `after-selection-menu.png` under `media/pr/tables-erd/`.

### Visual loading indicator

The existing `Spinner.vue` now occupies the diagram area while the catalog, lazy canvas component, and initial node layout load. `TablesCanvas` emits ready after the initial positions and viewport reach the DOM; the selector then reveals it. Empty graphs become ready on mount. Catalog failure hides the spinner so Retry remains usable; reloading resets readiness.

The delayed-catalog browser regression fails on the previous production build (no diagram loading indicator), then passes on the change. Evidence: `before-visual-loading.png`, `after-visual-loading.png`, `after-visual-ready.png`, and `visual-loading.gif` in `media/pr/tables-erd/`.

### Columns click destination

The reported symptom was a Columns click appearing to only focus its table. In this checkout, the existing handler did expand the side-panel list; the confirmed gap was that focus stayed on the card and there was no explicit reset/reveal of the destination. The new regression reproduced that gap (`Search columns…` was not focused). The handler now expands the list, clears its previous search/pagination, resets the details scroll position, and focuses column search after rendering. A focus-ID check prevents a late callback from focusing another table's input.

Two browser scenarios passed: switching/collapsing/reopening columns and the laptop/full-screen 300-column case. Evidence: `before-column-button.png`, `after-column-button.png`, and `column-button.gif` under `media/pr/tables-erd/`. This validates an explicit side-panel destination; it does not claim the originally reported complete failure to expand was reproduced.
