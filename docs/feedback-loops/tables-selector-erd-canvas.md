# Tables selector ERD — implementation and local feedback loop

Adds a minimal **Table / ERD** toggle to the existing selector. Both views use
one draft selection and the existing Save/Save & Continue operation. Selecting a
table reveals its direct declared or suggested neighbors without selecting them. The diagram
supports focus, one-hop expansion, search, key/relationship details, and optional
usage, last-used, and answer-feedback overlays.

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
| Shown, not selected | Gray dotted unchecked card with Add | Disabled |
| Not shown, not selected | Discoverable through search or Expand | Disabled |

- Active tables always bring their immediate incoming and outgoing neighbors
  into the graph model. Already selected neighbors stay checked.
- A revealed ghost does not recursively reveal its own neighbors. Selecting it
  or explicitly expanding it reveals the next hop.
- Focus, search preview, expand, drag, and overlays do not activate tables.
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
| `frontend/components/datasources/TablesCanvas.vue` | Vue Flow viewport, focus/expand, viewport rendering, selection menu, packed layout, overlays, detail panel |
| `frontend/components/datasources/TableCanvasNode.vue` | Minimal checked/ghost table card and explicit activation controls |
| `frontend/utils/tableGraph.ts` | Connection/schema-scoped relationship resolution, one-hop visibility, shared filter predicates |
| `frontend/components/datasources/{CatalogSelector,AgentKnowledgeTabs}.vue` | One combined onboarding selector across table connections; preserve the step when Save fails |
| `backend/app/schemas/datasource_table_schema.py` | Optional last-used timestamp with the existing UTC serializer |
| `backend/app/services/data_source_service.py` | Populate last-used from table stats; stable ID tie-breaker for pagination |
| `locales/{en,es,he,fr,sv,ar,ru,de,pt,it}.json` | Matching translated ERD namespace |
| `frontend/nuxt.config.ts` | Optional `BOW_API_TARGET` override for an isolated local backend |

Vue Flow was already installed and registered. Dagre's existing `dagre-d3-es`
package is now declared directly at the already-locked version, 7.0.14, and only
its layout/graph modules are imported. The ERD component loads asynchronously.
Initial layout uses fixed card geometry; explicit Arrange recomputes it.
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
- [x] Add usage/last-used/feedback overlays and all ten locale catalogs.
- [x] Add a deterministic fixture, focused Playwright suite and graph unit checks.
- [x] Run existing backend pagination, selection and reader-permission regressions.
- [x] Run the existing 30-case locale sweep and check catalog drift.
- [x] Complete the baseline-to-feature rerun and inspect before/after screenshots.
- [x] Complete the initial production build.
- [x] Refine selected/ghost strokes, reuse USelectMenu, and show connection icons.
- [x] Add labelled key-name suggestions and combined multi-connection onboarding.
- [x] Replace the node cap with viewport rendering and packed large-graph layout.
- [x] Finish refinement acceptance and production build.

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
| Overlay | `after-overlays.png` |
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
