# Excel integration review — sandbox feedback loop findings

Date: 2026-09-01. Review pass over the Excel add-in integration (AI tools,
Office.js bridge, taskpane, frontend plumbing), validated end-to-end in the
local sandbox. This doc records what was verified working and the issues
found, each with a reproduction. **Update:** fixes for issues 1–4, 5 (dead
page), and 6 have since been applied and re-verified in the same sandbox —
see "Fixes applied and verified" at the end.

## Scope reviewed

- Backend tools: `read_excel_range`, `read_excel_as_csv`, `write_to_excel`,
  `write_officejs_code` (`backend/app/ai/tools/implementations/`), shared
  bridge (`officejs_bridge.py`, `officejs_registry.py`), result endpoint
  (`POST /api/completions/{id}/tool-results/{tool_call_id}`).
- Prompting: platform gating in `app/ai/registry.py`, excel sections in
  `prompt_builder.py` / `prompt_builder_v3.py`, observation digests in
  `message_context_builder.py` (`_digest_excel_tool`).
- Add-in serving: `backend/app/routes/excel.py` (manifest, commands,
  self-contained taskpane HTML).
- Frontend: `composables/useExcel.ts`, `pages/reports/[id]/index.vue`
  (excel_action forwarding + `handleOfficeJsResult`), `PromptBoxV2.vue`,
  tool display components, `pages/excel/index.vue`, nuxt `/excel` proxy.

## Sandbox verification method

Booted backend + frontend per the sandbox-feedback-loop skill. The real
Anthropic key in the sandbox was out of credits (verified with a direct
`/v1/messages` call), so after confirming the real-LLM path reaches the
provider, the loop was driven by a deterministic OpenAI-compatible stub LLM
(`read_excel_as_csv` → `write_to_excel` → final answer) registered as a custom
provider. Since Office can't run in the sandbox, a **taskpane emulator** was
injected into the report page via Playwright `addInitScript`: it implements the
taskpane's postMessage contract (`runOfficeJs` / `applyToExcel` /
`cancelOfficeJs` → `officeJsResult`) and executes the tool-generated Office.js
against a mock `ctx.workbook` holding a 4×3 sheet. Because the report page
posts to `window.parent` (which is `window` itself when unframed) with
same-origin targetOrigin, the emulator satisfies the page's
`event.source === window.parent` / same-origin checks exactly like the real
taskpane does.

## Verified working (single-worker)

- Platform gating: the 4 excel tools appear only when
  `platform == "excel"` (`registry.py` filter checked directly; 72 tools for
  web vs 76 for excel, no leakage either way).
- `?excel=true` → sticky excel mode → `platform: 'excel'` +
  `platform_context` (selection snapshot) sent on completion create;
  `completions.external_platform` persisted as `excel`; report gets bound to
  the Excel external platform.
- Full read loop: planner tool call → SSE `tool.partial` with `excel_action`
  (code + echoed `completion_id`) → page forwards to taskpane → generated
  Office.js executed correctly against the mock workbook (load/sync usage,
  `maxRows` truncation resize, CSV quoting) → result POSTed to
  `/tool-results/{tool_call_id}` → future resolved → observation
  (`Read Sheet1!A1:C4 as CSV`) → CSV content visible to the next planner
  round (the stub keyed phase 2 off the CSV text, proving the observation
  round-trip).
- CSV persistence: `uploads/files/<uuid>_excel_Sheet1_A1_C4.csv` written with
  exact sheet content, `files` row created with preview, linked via
  `report_file_association`.
- Write loop: `write_to_excel` `tool.finished` forwards `applyToExcel` to the
  taskpane with normalized columns/rows; emulator received the correct
  payload shape for `appendDataToExcel`.
- UI: tool cards ("Read Sheet1!A1:C4 as CSV", "Wrote 1 row × 2 cols to
  Excel"), final answer, and the "excel report viewed outside Excel" banner
  (`reports/[id]/index.vue:781`) all render.
- Cancel path: sigkill/timeout emits `cancelOfficeJs` on `tool.progress`; the
  taskpane keeps a cancelled-id set and drops late results.

## Issues found

### 1. CRITICAL — Office.js bridge breaks under multi-worker deployment

`pending_officejs_registry` is an in-process dict of asyncio futures
(`officejs_registry.py`, whose own docstring says "Single-process only").
The tool awaits the future on the worker streaming the completion, but the
taskpane's `POST /api/completions/{id}/tool-results/{tool_call_id}` lands on a
random worker. Production runs multiple workers: `start.sh` computes
`min(CPUs/2, 4)` and passes `--workers` to uvicorn (and `backend/main.py`'s
`__main__` block even says `workers=20`, ignored only because `reload=True`).
On any host with ≥4 CPUs, most Excel tool round-trips will miss the owning
worker: the POST 404s, the tool hangs 55 s
(`officejs_bridge.DEFAULT_WAIT_TIMEOUT_S`), then fails with "Timed out waiting
for Excel taskpane to return a result".

**Reproduced** in the sandbox with `uvicorn main:app --workers 2`: after several
same-worker (keep-alive-sticky) successes, a run hit the other worker —
frontend got `404 {"detail":"No pending tool call with that id ..."}` and the
backend logged `officejs tool-result arrived for unknown/closed
tool_call_id=... Likely the tool already timed out or was cancelled` while the
tool was in fact still pending on the sibling worker. The frontend does not
retry the POST, so the result is lost.

Note the codebase already solves this exact problem for MCP tool
confirmations by writing to `tool_confirmations` and polling the DB — the
comment in `routes/completion.py` (`respond_to_mcp_tool_confirmation`)
explains: "this request rarely lands on the worker streaming the completion".
The officejs bridge needs the same treatment (DB row or Redis pub/sub).

Repro note (sandbox-specific): when running >1 worker locally you must set a
fixed `BOW_ENCRYPTION_KEY`, otherwise each worker generates an ephemeral key
and JWTs bounce between workers with 401s (production's `start.sh` already
generates the key before forking).

### 2. HIGH — `write_to_excel` reports success without any taskpane ack

Unlike the three bridge tools, `write_to_excel` is fire-and-forget: the tool
emits `tool.end` with `success: true` and the observation "Wrote N rows x M
columns to Excel" immediately (`write_to_excel.py`), and the *frontend* is
responsible for forwarding `result_json.excel_action` to the taskpane — only
on a live SSE `tool.finished` event, only if the report page is open, and only
if `isExcel` (`reports/[id]/index.vue:3565`). There is no result round-trip.

Consequences:
- If the taskpane/page isn't listening (page reloaded mid-run, SSE dropped,
  session not actually inside Excel), nothing is written but the model and
  the user are told the write succeeded. **Reproduced**: with excel mode
  sticky but no taskpane present, the run produced "Wrote 1 row × 2 cols to
  Excel" + a final answer claiming the table was written; the `applyToExcel`
  payload had no receiver.
- On completion reload/replay there is no re-forward, so the write cannot be
  recovered.
- `appendDataToExcel` in the taskpane writes at `getSelectedRange()` — the
  live cursor at execution time. The officejs cheatsheet warns at length that
  the cursor is stale/unsafe for `write_officejs_code`, but `write_to_excel`
  always anchors there, and the tool's success summary doesn't say where the
  data landed.

Suggested direction (not applied): route `write_to_excel` through the same
dispatch/await bridge as the other tools so success reflects an actual ack
(and the landing address can be reported back).

### 3. HIGH — sticky Excel mode leaks into normal browser sessions

`useExcel.ts` marks the session sticky-Excel forever once any URL carries
`?excel=true` (module-load check writes `excelSticky=1` to localStorage;
sticky "never expires" and ignores heartbeats). localStorage is per-origin, so
for Excel-on-the-web users (taskpane iframe runs in the same browser profile)
— or anyone who ever opened a `?excel=true` link — every subsequent normal tab
on the BOW origin also claims `platform: 'excel'`. The excel tools are then
offered to the planner, dispatched to a nonexistent taskpane, and each call
hangs 55 s before failing; the planner retries and burns more rounds.

**Reproduced**: plain Playwright tab (no taskpane) with the sticky flag —
`read_excel_as_csv` hung ~55 s, surfaced "Timed out waiting for Excel taskpane
to return a result", and the planner immediately re-tried the read until the
run was stopped. The only escapes are `?excel=false` or a debug
`setExcelStatus(false)`; there is no UI affordance, and the taskpane-liveness
heartbeat that exists (`excelInitialized` every 5 s) is deliberately ignored
in sticky mode.

Related hardening gap: `handleExcelMessage` (excel-mode + selection intake)
accepts postMessages from **any origin/source** — unlike `handleOfficeJsResult`
which checks both. Any page that can frame or open the app can flip it into
excel mode or spoof `cellSelected` values that end up in the prompt as
`<excel_context>`.

### 4. MEDIUM — Excel users with no data sources cannot submit at all

`canSubmit` requires `hasDataSourceOrFile` (`PromptBoxV2.vue:1335-1349`) —
some org data source or an uploaded file — even when the session is
platform=excel and the question targets the live workbook (which needs no
data source; the excel tools are self-sufficient). **Reproduced**: fresh org
in excel mode, typed a question about the sheet → send button disabled,
tooltip "Connect data or upload a file". A brand-new workspace opening the
add-in is dead in the water until someone connects an unrelated data source.

### 5. MEDIUM — dead/broken legacy Excel surfaces

- `frontend/pages/excel/index.vue` (+ `layouts/excel.vue`) is unreachable:
  the nuxt proxy rewrites `/excel/*` → backend `/api/excel/*`
  (`nuxt.config.ts:129`), which only serves
  `manifest.xml` / `commands.html` / `taskpane.html`. Verified:
  `GET /excel` → 404 JSON, while `GET /excel/taskpane.html` → 200. The page
  also links to `/excel/reports/{id}`, a route that exists nowhere (404
  verified). Either the page should be deleted or the routes reconciled.
- `backend/app/ai/agents/excel/excel.py` (`ExcelAgent`, LLM schema inference
  over uploaded workbooks) is legacy: its only caller is the deprecated
  `file_service._create_sheet_schemas_legacy`. It `json.loads` raw LLM output
  with no fence-stripping/retry, uses sync inference, and its prompt requests
  a malformed schema shape — fine to keep as dead code, but it is not part of
  the live excel integration and shouldn't be mistaken for it.

### 6. MEDIUM (security) — `submit_tool_result` is resolvable by any org member

`completion_service.submit_tool_result` only enforces the initiating user when
`completion.user_id` is set — but the `completion_id` echoed in
`excel_action` is the **system** completion, and system completions have
`user_id = NULL` (verified in DB), so the ownership check never applies.
Additionally the endpoint doesn't verify that `tool_call_id` belongs to
`completion_id` (any known completion id in the org works). Pending
`tool_call_id`s are broadcast in the report's SSE stream (`excel_action`
carries them), so any org member who can view the report — not just the
prompt author — can POST a forged `officeJsResult` and feed the agent
fabricated spreadsheet contents mid-run. Low likelihood, but the fix is
cheap: bind the pending future to the initiating user id and the owning
completion at registration time and check both on resolve.

### 7. LOW — cross-origin "Configure URL" silently breaks the bridge

The taskpane's settings overlay lets users point the add-in at any BOW URL
(`routes/excel.py`). If that URL's origin differs from the origin serving
`taskpane.html`, the bridge dies silently: the report iframe posts
`officeJsResult`-bound messages to `window.parent` with
`targetOrigin = its own origin` and requires `event.origin === its own origin`
(`reports/[id]/index.vue:3456,4212`), both of which fail for a cross-origin
parent. Meanwhile excel-mode detection still works (issue 3's unchecked
origin), so tools are offered and every call times out. If cross-instance
configuration is meant to be supported, the origin handshake needs to carry
the taskpane origin; if not, the settings form should refuse cross-origin
values.

### 8. LOW — misc

- `read_excel_as_csv` links its persisted CSV to the report without
  `completion_id` (`report_file_association.completion_id` NULL — verified),
  losing the turn attribution that uploaded files get.
- `officejs_bridge.DEFAULT_WAIT_TIMEOUT_S = 55` silently undercuts the
  advertised `timeout_seconds=60` in the read tools' metadata; a legitimate
  long-running Office.js execution (huge used range on a slow machine) that
  finishes at ~56 s gets a 404 on its POST and is reported as timed out. The
  late-result drop is only race-safe when the cancel notification actually
  reached the taskpane.
- `pages/excel/index.vue` fetches with `if (!response.code === 200)` (always
  false — negation binds before comparison); moot while the page is dead
  code, but worth noting if it's ever resurrected.

## Suggested priorities

1. Issue 1 (multi-worker bridge) — production correctness of the whole
   feature; mirror the `tool_confirmations` DB-polling pattern.
2. Issues 2–4 — user-facing correctness/UX inside the add-in.
3. Issue 6 — cheap authz hardening.
4. Issues 5, 7, 8 — cleanup and hardening.

## Fixes applied and verified (same day)

### What changed

- **Issue 1 + 6 — durable, bound bridge.** New `officejs_pending_results`
  table (model `OfficeJsPendingResult`, migration `officejs01`, service
  `OfficeJsResultService`) mirroring the `tool_confirmations` pattern: the
  waiting tool registers a row (bound to the initiating `user_id` and the
  run's system `completion_id`), keeps the in-memory future as a same-worker
  fast path, and polls the row every 0.75s. `submit_tool_result` resolves the
  row first (validating the completion binding → 404 on mismatch, the user
  binding → 403 on mismatch, exactly-once → 404 on replay) and then pokes the
  in-memory registry. Rows are deleted when the tool stops waiting, so late
  POSTs 404 exactly as before. The result payload column uses `EncryptedJSON`
  like `tool_executions.result_json`.
- **Issue 2 — write_to_excel is now an acked round-trip.** The tool dispatches
  an `applyToExcel` excel_action (with `id`/`completion_id`) on `tool.partial`
  and awaits the taskpane's `officeJsResult` through the same bridge; the
  taskpane's `appendDataToExcel` acks with `wrote_to` (the actual landing
  address), which surfaces in the observation ("Wrote 1 rows x 2 columns to
  Excel at Sheet1!A6:B7") and the tool card. Without a taskpane the tool now
  fails with a clear timeout instead of reporting success. The report page
  forwards `applyToExcel` from `tool.partial` (like `runOfficeJs`) and the
  old `tool.finished` forward was removed; the taskpane still accepts legacy
  id-less payloads (old "Add to Excel" buttons) fire-and-forget.
- **Issue 3 — sticky Excel mode is tab-scoped.** `useExcel.ts` stores the
  sticky flag in `sessionStorage` (scoped to the taskpane iframe / tab)
  instead of `localStorage` (origin-wide); ordinary page loads also clean up
  the pre-fix `localStorage` flag so previously-stuck browsers migrate out.
  `handleExcelMessage` now requires `event.source === window.parent` and a
  same-origin `event.origin`, closing the excel-mode/selection spoof.
- **Issue 4 — gating.** `hasDataSourceOrFile` counts Excel mode as having a
  data source (the live workbook), so a fresh workspace can prompt from the
  add-in; ordinary web sessions still require a data source or file.
- **Issue 5 — dead page removed.** `frontend/pages/excel/index.vue` and
  `layouts/excel.vue` deleted (both unreachable behind the `/excel` proxy).
  `ExcelAgent` is left in place as documented legacy behind the deprecated
  file_service path.
- Cross-worker resolutions log an INFO line ("resolved via durable row only")
  for operational visibility.

Deliberately not changed: issue 7 (cross-origin "Configure URL") beyond the
origin checks above — cross-instance configuration remains unsupported; and
issue 8's `completion_id` attribution for tool-persisted CSVs — the
mark-images code in completion_service deliberately treats tool-created files
as NULL-completion rows (claiming them displays them as user attachments), so
matching `write_csv`'s existing behavior is correct for now. The 55s-vs-60s
timeout margin is unchanged (it is what keeps the timeout inside the runner's
hard limit).

### Verification (sandbox, 2-worker uvicorn, stub LLM + taskpane emulator)

- **Multi-worker bridge:** 6/6 full read+write+answer runs passed under
  `--workers 2`; all 12 `/tool-results/` POSTs returned 200 (previously 404
  on cross-worker routing). Backend log recorded **7 resolutions via the
  durable row only** — POSTs that landed on the worker not running the
  completion, i.e. exactly the case that used to fail. Pending rows are
  cleaned up after every call (`officejs_pending_results` empty at rest).
- **Acked write:** success path shows "Wrote 1 row × 2 cols to Excel at
  Sheet1!A6:B7"; with no taskpane the tool fails after the bridge timeout
  with "Timed out waiting for the Excel taskpane to confirm the write…" —
  the false success from the review is gone.
- **Sticky scoping:** with a pre-fix `localStorage.excelSticky=1` seeded, a
  plain tab comes up non-Excel (flag migrated away, submit gated on data
  sources as before); a `?excel=true` tab gets `sessionStorage` sticky and
  Excel mode; a further plain tab opened afterwards does not inherit it.
- **Gating:** in a workspace with zero data sources, the `?excel=true` tab
  can submit a prompt (and the full excel tool loop runs datasource-free);
  plain tabs still show "Connect data or upload a file".
- **Authz binding:** unit tests (`tests/unit/test_officejs_result_service.py`,
  5 passed) cover wrong-completion → not_found, wrong-user → forbidden,
  exactly-once resolution, discard-then-late-POST → not_found, and the
  cross-session resolve→poll path. Related suites
  (`test_tool_confirmation_service.py`,
  `test_message_context_tool_result_projection.py`,
  `test_read_file_source_document.py`) still pass (53 tests).
