# Feedback Loop — "it takes a long time until the artifact is loaded" (/reports/:id, /r/:id)

On app.bagofwords.com an artifact took ~4.2s to appear on `/r/:id` and ~5.7s
on `/reports/:id`. No single request was slow: the time was a chain of steps
that each waited for the previous one. This loop reproduces the chain on a
seeded sandbox, changes it, and proves the dashboards render **exactly** as
before — same DOM, styles, interactions, messages, screenshots — for every
role on both surfaces.

## Root cause (validated)

1. **The iframe never uses the HTTP cache.** It is sandboxed without
   `allow-same-origin` (`frontend/pages/r/[id]/index.vue:216`,
   `frontend/components/dashboard/ArtifactFrame.vue:419`), i.e. an opaque
   origin. Chrome gives opaque-origin frames a transient cache partition, so
   every view re-downloaded every library. Validated in isolation: a sandboxed
   `srcdoc` iframe loading a script served `Cache-Control: immutable` hit the
   server on every load (3/3); the same iframe with `allow-same-origin` hit
   it once. On the live stack: 13 `/libs` requests per view, every view.
   *So a Cache-Control header alone (the original idea for this change)
   cannot help the iframe.*
2. **Libraries loaded one after another, after all data.** 11 `<script src>`
   tags in the iframe (`utils/artifactIframe.ts`, pre-change) were fetched
   serially, and the iframe is only created once every data request finished.
3. **babel-standalone compiled the artifact on every view** (2.3MB raw /
   575KB gz download + parse + transform).
4. **`/r` data took four sequential rounds**: report+artifact list → artifact
   detail → query list → one step per query.
5. **`/reports` started the dashboard late**: `ArtifactFrame` mounts only after
   the workspace loads and a full page render, then its queries request queued
   behind ~20 requests the rest of the page fires at that moment.

## Loop A — deterministic before/after harness (no external services)

```bash
tools/agent/boot_stack.sh
cd backend && uv run python ../tools/agent/seed_org.py --invite viewer@example.com
TESTING=true ENVIRONMENT=production TEST_DATABASE_URL=sqlite:///db/agent.db \
  uv run python ../tools/agent/seed_artifact_perf.py > /tmp/perf_seed.json   # strip log lines
# serve the generated SPA from FastAPI exactly like production (SERVE_FRONTEND=1),
# old build on :8001, new build on :8002; put production-like gzip in front:
node tools/agent/gzip_proxy.mjs 9001 8001 & node tools/agent/gzip_proxy.mjs 9002 8002 &
python3 tools/agent/artifact_perf_reset.py /tmp/perf_seed.json backend/db/agent.db
node tools/agent/artifact_perf_harness.mjs --seed /tmp/perf_seed.json --out out/before --origin http://127.0.0.1:9001
python3 tools/agent/artifact_perf_reset.py /tmp/perf_seed.json backend/db/agent.db
node tools/agent/artifact_perf_harness.mjs --seed /tmp/perf_seed.json --out out/after  --origin http://127.0.0.1:9002
python3 tools/agent/artifact_perf_compare.py out/before out/after --md compare.md
```

`seed_artifact_perf.py` seeds 8 reports with deterministic data (step code
reproduces its own snapshot, so refresh-on-view never changes a page):

| name | what it exercises |
|---|---|
| exec | themed runtime, 5 KPICards, 3 EChart charts, Segmented tabs, 1,000-row searchable DataTable, Icon, private class fields, generators, async effects, unicode/HTML-ish cell text |
| params | `useParams` + `FilterSelect` fed by an options-source query, and an **identity-scoped** query (withheld for non-owners, auto-run for members) |
| edge | `import` lines, class components + error boundary, async iteration — and a **newer doc** artifact on the same report |
| syntax | JSX syntax error (Babel fallback path) |
| runtime | throws during render |
| files | embedded PNG + PDF through `BowFile` |
| slides | legacy HTML slides |
| legacy | raw `echarts.init` + `window.lucide`, no runtime version |

The harness loads each on `/r` (owner, member, anonymous where public) and
`/reports` (owner, member), cold and warm cache, at 60ms latency / 30 Mbps,
and records: first artifact paint, `ARTIFACT_READY`/`ERROR`, the request
waterfall, a fingerprint of the rendered iframe (text, per-`data-testid`
text and computed style, element counts, painted canvases), interaction
results (tab switches, table search, button clicks, a region filter that
re-runs a query), host banners and failing responses, plus a screenshot.

`artifact_perf_reset.py` exists because two things depend on history, not
code: a member's parameter run is cached per viewer and served on their next
load, and an owner visit reruns the report at most once per 5 minutes
(wall-clock claim) — a rerun deletes viewers' cached slices. Without the
reset, case order and wall-clock timing flipped a member's initial total
between runs; with it, both runs start identical.

## Results (gzip proxy, 39 cases × cold + warm = 78 loads)

**Behavior: 0 regressions.** Every successful dashboard is identical —
rendered text, styles, element counts, interaction results (e.g. the region
filter: 762,000 → 152,400 in both), withheld/auto-run behavior per role,
screenshot pixel diff 0.00%. Error cases fail the same way; where the old
path reported the browser-masked `"Script error."` (thrown inside a
cross-origin `/libs` script), the new one reports the real message, e.g.
`Cannot read properties of undefined (reading 'deep')`.

Median, ms from navigation start:

| surface | cache | first paint before → after | ready before → after | /libs bytes per view |
|---|---|---|---|---|
| /r | cold | 2826 → 2565 | 3547 → 3090 | 1.26MB → 0.65MB |
| /r | warm | 2206 → 1787 | 2659 → 2242 | 1.15MB → 0.09MB |
| /reports | cold | 4948 → 4573 | 5400 → 5114 | 1.28MB → 0.91MB |
| /reports | warm | 3150 → 2842 | 3573 → 3276 | 1.33MB → ~0 |

The artifact's own share (first API request → first paint), which excludes
the unchanged app boot: `/r` cold 2144 → 1674 (−22%), warm 1754 → 1328
(−24%); `/reports` cold 2992 → 2576 (−14%), warm 2502 → 2204 (−12%).

Without compression (uvicorn alone) the cold numbers did **not** improve and
`/reports` cold got slower: 3.3MB of uncompressed dev-React libs downloading
in parallel starved the data requests on the throttled link. Production
compresses (Caddy `encode gzip`, the live site serves gzip), so the gzip
proxy is the representative measurement; lib fetches also run at
`priority: 'low'`.

## The fix

- **Host-fetched, inline libraries** — `frontend/utils/artifactLibs.ts`
  fetches the libs from the page (same origin → normal HTTP cache) in
  parallel at mount; `buildArtifactIframeHtml` runs each as an inline script
  in the original order (`INLINE_RUNNER`, `utils/artifactIframe.ts:256` — the
  source travels as a JSON string and is set via `.text`, so lib text is never
  parsed as HTML). A failed/timed-out fetch falls back to `<script src>`.
  Babel stays a `<script src>` (inlining it made the fallback path ~1.7s
  slower), as does pdf.js, which now loads only when the code mentions a PDF
  or embeds one (`needsPdf`, `:201`).
- **Hashed, immutable `/libs`** — `nuxt.config.ts` hashes `public/libs/*` at
  build into `__BOW_LIB_HASHES__`; `libUrl()` (`:146`) appends `?h=`;
  `backend/app/core/spa.py:94` serves hashed lib URLs immutable, unhashed ones
  `no-cache`.
- **Precompiled JSX** — `backend/app/services/artifact_precompile.py` compiles
  page code with the vendored babel-standalone and **exactly** the options
  `transformScriptTags` uses (`_COMPILE_SCRIPT`, `:70`), caches it in memory +
  on disk (key: source + Babel build + options version), never on the request
  path (eager on artifact create/edit, background on a read miss, backoff when
  no Chromium, rejected sources remembered). The read routes return
  `compiled_js` + `compiled_js_key`; the iframe uses it only when the key
  (`precompiledKey`, `:173`, FNV-1a over UTF-16, mirrored by `source_key`)
  matches the code being rendered, and runs it on DOMContentLoaded from a
  fresh `<head>` script registered where the Babel tag was
  (`precompiledLoaderScript`, `:263`) — same timing, scope, listener order and
  artifact-globals import stripping as Babel.
- **`GET /api/r/{id}/queries_with_steps`** (`routes/report.py:723`) — each
  step built by the same `_build_public_step` (`report_service.py:2455`) the
  per-step route uses; `/r` fetches it alongside the artifact detail
  (`pages/r/[id]/index.vue:772`), with the old endpoints as fallback.
- **`/reports` prefetch** — `startArtifactFramePrefetch`
  (`pages/reports/[id]/index.vue:5115`) starts the frame's queries, viewer
  context and libs as soon as the artifact id is known, initial load only;
  `ArtifactFrame` consumes each once, only for the matching artifact, never in
  verification preview.

## Tests

| test | proves | can fail? |
|---|---|---|
| `backend/tests/unit/test_artifact_precompile.py` | server output is byte-identical to in-browser `transformScriptTags` (JSX, private fields, generators, async iteration, imports, unicode); key vectors shared with the frontend; shape rules; cache never pairs output with other code; backoff; rejected sources | dropping the `env` preset → 3 failures |
| `backend/tests/e2e/test_public_queries_with_steps.py` | batched view == list + per-step view for anonymous / owner / member / outsider, with and without a member's own run; identity rows withheld; visibility gate identical; HTTP route | passing no user in the batched path → 3 failures |
| `backend/tests/unit/test_spa_lib_caching.py` | hashed → immutable, unhashed/garbage → no-cache, other static policies unchanged, frontend emits `h=` | — |
| `frontend/tests/unit/artifactPrecompiled.mjs` | in real Chromium: inline libs, inline+precompiled, and a stale key all render the same DOM, messages and DOMContentLoaded order as the original path; precompiled runs exactly Babel's output | running precompiled code immediately instead of on DOMContentLoaded → fails |

Existing suites re-run green: frontend `artifactOpaqueFrame`,
`artifactFormIsolation`, `artifactResourceSdk`, `artifactVerificationDelivery`,
`polishPicker`; backend unit `-k "artifact or spa or public"` and e2e
`-k "artifact or public or rerun or doc_ or html_export"`.

## What this proves / limits

- Behavior parity is measured, not assumed, across 8 artifact kinds × 2
  surfaces × 3 roles × cold/warm, plus the Chromium-level parity test.
- Timings are a sandbox with emulated network; production adds real RTT
  (the old path paid ~11 serial round trips for libs, so it gains more there)
  and a CDN.
- Remaining critical path: running ~2MB of library code inside the iframe and
  React's first render (~0.8s). Next levers: production React on `/reports`
  (it uses the development build, 1.2MB, for readable errors), loading
  ECharts/Lucide only when used, and the ~2s app boot before the first API
  call.
- Found along the way (unchanged here): artifacts using
  `import { x as y } from 'react'` break because import lines are stripped
  after compilation; a member's cached parameter slice renders while the
  filter control still shows "All"; `GET /api/artifacts/{id}/runtime/context`
  404s on every view of a non-resource artifact.
