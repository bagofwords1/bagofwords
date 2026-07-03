# Sandbox Feedback Loop — Public report/artifact pages extremely slow with large data

Reproduces the reported issue: opening a published report (`/r/{id}`) whose
queries hold a lot of data is **extremely slow** — in the reported trace,
`GET /r/{id}` took **18.4 s** and `GET /r/{id}/artifacts` **17.6 s** even though
both responses are ~1–2 kB, and the page then sits on "Loading..." while each
query's `/step` is fetched one at a time.

This doc is the runnable feedback loop used to confirm the root cause in a
fresh cloud sandbox.

---

## Root cause (validated)

Three independent behaviors combine:

1. **Mapper-level selectin cascade.** Every relationship on `Report` is
   `lazy="selectin"` (`backend/app/models/report.py:58-75`), and so are
   `Query.steps` / `Query.default_step` / `Step.query`
   (`query.py:26-27`, `step.py:41`). A plain `select(Report)` therefore eagerly
   hydrates the **entire report graph**: all queries → **all step versions**,
   each carrying the full query result rows in the `steps.data` JSON column —
   plus every artifact version's content, all completions, widgets,
   visualizations, shares, stars, …

2. **Every public endpoint pays it — including the visibility check.**
   `get_public_report`, `get_public_artifacts`, `get_public_artifact`,
   `get_public_queries` and `get_public_step`
   (`backend/app/services/report_service.py:875-1068`) each begin with
   `select(Report)` **just to check `artifact_visibility`**, so even the tiny
   artifacts-list response hydrates every stored dataset. `GET /r/{id}`
   additionally re-selects the `Report` a second time for fork eligibility
   (`backend/app/routes/report.py:362`) — the cascade runs **twice** in one
   request. All of this JSON hydration is CPU-bound Python on the asyncio
   event loop, so concurrent requests stall each other too.

3. **Version amplification + serial frontend waterfall.** `Query.steps` loads
   **every historical step version** (each a full copy of the dataset) though
   only the default step is ever served; and the public page fetches each
   query's `/step` **sequentially** in a for-loop
   (`frontend/pages/r/[id]/index.vue:376-378`), multiplying the per-request
   cascade by the number of queries before anything renders (`dataReady`
   gates the iframe).

---

## Environment setup (fresh sandbox)

The app targets **Python 3.12** (the sandbox default `python` may be 3.11).

```bash
cd backend
pip install uv
uv sync --frozen --extra dev --python /usr/bin/python3.12

# Required by bow-config.dev.yaml (database.url: ${BOW_DATABASE_URL})
export BOW_DATABASE_URL="sqlite:///db/app.db"
mkdir -p db
```

Tests run on SQLite by default; the autouse `run_migrations` fixture builds the
schema per test (`tests/conftest.py`).

---

## Loop A — App-logic reproduction (no LLM / live data source needed)

Self-contained: seeds a **public** report with 4 queries × 3 step versions
(each version storing ~2.7 MB of result rows in `steps.data`, i.e. what "an
artifact with a lot of data" looks like) plus 3 artifact versions, then

- runs a bare `select(Report)` under a SQL recorder (Claims 1 & 3), and
- replays the exact browser waterfall of `frontend/pages/r/[id]/index.vue`
  against the public endpoints, timing each request and counting SQL
  (Claim 2), against both a small and a large report of identical shape.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
.venv/bin/python -m pytest tests/e2e/test_artifact_large_data_perf_repro.py -v -s
# dataset size per step version is tunable: BOW_REPRO_ROWS=30000 ... (default 15000)
```

**Observed (PASS) — cascade (Claims 1 & 3):**

```
[cascade] select(Report) issued 40 SQL statements in 2.27s
[cascade] tables hit: {'steps': 7, 'entities': 7, 'external_user_mappings': 5,
  'reports': 3, 'queries': 3, 'visualizations': 3, 'widgets': 3, 'artifacts': 1,
  'scheduled_prompts': 1, 'report_shares': 1, 'report_stars': 1, 'text_widgets': 1,
  'completions': 1, 'dashboard_layout_versions': 1, 'users': 1, 'organizations': 1}
[cascade] steps hydrated: 12 (= 4 queries x 3 versions; only 4 default steps are ever served)
[cascade] step data hydrated: 32.6 MB; artifact content: 0.3 MB
```

A one-row `select(Report)` fans out to 40 statements and hydrates **32.6 MB**
of step data — 3× more than needed even if data were wanted, because all
historical versions load.

**Observed (PASS) — endpoints (Claim 2), small vs large report:**

```
[endpoints] per-step dataset: small=2 kB, large=2.7 MB (4 queries x 3 step versions stored)
[endpoints] endpoint                                   small     large   ratio  resp(large)   SQL steps-SQL
[endpoints] GET /r/{id}                                417ms    4563ms   10.9x        1.9kB    81        14
[endpoints] GET /r/{id}/artifacts                      173ms    4498ms   26.0x        0.7kB    80        14
[endpoints] GET /r/{id}/artifacts/{aid}                128ms    4500ms   35.1x      110.6kB    80        14
[endpoints] GET /r/{id}/queries                        234ms    6560ms   28.0x        1.2kB   126        19
[endpoints] 4 serial /step calls                       794ms   13824ms   17.4x     9902.6kB   488        52
[endpoints] full page waterfall: small=1746ms large=33945ms
[endpoints] GET /r/{id} SQL statements: 81 (~2x the artifacts list's 80: cascade runs twice)
```

This is on **local SQLite with zero network latency** — the same page load is
**1.7 s** for a small report and **34 s** for the large one, matching the
reported trace shape exactly: the **0.7 kB** artifacts-list response takes
**4.5 s** (reported: 1.2 kB / 17.6 s on production Postgres), and each request
re-queries the `steps` table 14–19 times.

---

## Loop B — Live browser reproduction (backend + frontend + Playwright)

Runs the real stack and drives the public report page in headless Chromium,
recording exactly what the reporter's DevTools screenshot shows: tiny
responses, multi-second latencies, and a long "Loading..." before the
artifact appears. The artifact is REAL — generated with the app's own
codegen (`app/services/artifact_codegen.py`, same path as the
"add visualization to dashboard" endpoint) and renders 4 ECharts from
`window.ARTIFACT_DATA`.

```bash
# 1. Backend (fresh dev DB + seed two public reports: small=50 rows/step,
#    large=15000 rows/step; both 4 queries x 3 step versions + 1 artifact)
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db"
rm -f db/app.db && .venv/bin/python -m alembic upgrade head
.venv/bin/python scripts/seed_artifact_perf_repro.py   # prints SMALL/LARGE_REPORT_ID
.venv/bin/python -m uvicorn main:app --host 127.0.0.1 --port 8000 &

# 2. Frontend (proxies /api -> :8000) + vendored iframe libs
#    (React/Babel/ECharts are not in-repo; without them the artifact iframe
#    stays on "Loading..." forever)
bash scripts/download-vendor-libs.sh frontend/public/libs
cd frontend && yarn install && yarn dev &

# 3. Playwright (uses the sandbox's preinstalled Chromium)
npm i playwright-core   # anywhere; pass via NODE_PATH
NODE_PATH=<that>/node_modules FRONT=http://localhost:3000 \
  SMALL=<id> LARGE=<id> OUT=/tmp/pw \
  node backend/scripts/pw_artifact_perf_repro.js
```

**Observed (browser-side, headless Chromium against the dev stack):**

```
=== small report ===  iframe (all API data fetched): 4649 ms, charts rendered: 4973 ms
=== large report ===  iframe (all API data fetched): 37064 ms, charts rendered: 59184 ms
api requests (large; sorted by duration — compare with the reported DevTools trace):
     9080 ms  200        0.3 kB  /api/r/{id}/artifacts
     9043 ms  200        1.8 kB  /api/r/{id}
     6339 ms  200        1.3 kB  /api/r/{id}/queries?artifact_id={uuid}
     4566 ms  200        3.7 kB  /api/r/{id}/artifacts/{uuid}
     4188 ms  200     2388.4 kB  /api/r/{id}/queries/{uuid}/step
     3404 ms  200     2388.4 kB  /api/r/{id}/queries/{uuid}/step
     3329 ms  200     2388.4 kB  /api/r/{id}/queries/{uuid}/step
     3295 ms  200     2388.4 kB  /api/r/{id}/queries/{uuid}/step
=== summary ===
time to artifact iframe: small=4649 ms, large=37064 ms (8.0x slower)
```

Same fingerprint as the report: `GET /r/{id}` **9.0 s for 1.8 kB** and the
artifacts list **9.1 s for 0.3 kB** (reported: 18.4 s / 2.2 kB and
17.6 s / 1.2 kB on production Postgres + network — this sandbox runs SQLite
on localhost, so production is ~2x worse). The page sits on "Loading..."
until report + artifacts + every query's `/step` complete, then spends
**another ~22 s** parsing/rendering the ~10 MB of rows inlined into the
iframe `srcdoc` before the charts paint (59 s total; the small report paints
at 5 s).

---

## What this proves

- The slowness is **not** the artifact payloads or the network: responses are
  tiny; the time is server-side hydration of `steps.data` (and artifact/
  completion history) triggered by any `select(Report)`. The artifact itself
  never uses that data — its endpoints return metadata/code only, yet a real
  browser sees them take ~9 s each (Loop B).
- **Every** `/r/{id}/*` endpoint pays the full cascade — even the pure
  visibility check — and `GET /r/{id}` pays it twice.
- All step **versions** load (N× data duplication), and the frontend's serial
  `/step` loop multiplies the cascade by the number of queries before render.

## Candidate fix (not yet implemented — for discussion)

1. Remove the mapper-level `lazy="selectin"` defaults on
   `Report`/`Query`/`Step` relationships (use lazy `select`/`raiseload` and
   opt in per-endpoint with explicit `selectinload()` where genuinely needed).
2. Mark `Step.data` as a `deferred()` column so result rows load only where
   they are actually served (`get_public_step`, step detail routes).
3. Make `_check_visibility` / fork-eligibility read only the columns they need
   (`artifact_visibility`, `user_id`, `organization_id`) instead of hydrating
   a full `Report`.
4. Frontend: fetch steps with `Promise.all` (or a batch endpoint) instead of
   the serial for-loop, and consider capping rows inlined into the iframe
   `srcdoc` (`frontend/utils/artifactIframe.ts:48`).

Items 1–3 should take the 4.5–18 s requests to milliseconds; the repro's
assertions (`artifacts list latency scales with stored data`,
`steps hydrated == queries × versions`) are written to **fail once the fix
lands**, flipping this loop into a regression test.
