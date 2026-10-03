# Artifact resource implementation and qualification

Status: local verification and initial real Luna/Sol completion and browser checks passed. Full repeated release qualification and production enablement remain pending.

Branch: `codex/artifact-resources`, based on `origin/main` at
`c56f60c8f6333b19ed8c5934debaf8b48d438552`. Existing artifacts and UI history
keep their identities. No persisted execution state or recovery API is added.

## Reproduction and verification

Before implementation, the resource contract test could not import the resource
schema module. Existing artifacts had no managed record/file/stream APIs or
inspection view. The regression log was captured before adding those modules.

Run from `backend` with the repository Python environment:

```sh
BOW_DATABASE_URL=sqlite:///db/app.db TESTING=true .venv/bin/python -m pytest tests/e2e/test_artifact_resources.py tests/unit/artifact_resources -q
```

From `frontend`:

```sh
node tests/unit/artifactResourceSdk.mjs
node tests/unit/artifactVerificationDelivery.mjs
node tests/unit/viewerRunHydration.mjs
node node_modules/nuxt/bin/nuxt.mjs build
```

The API suite uses real registration, sharing checks, group memberships,
resource services, migrations and database persistence. Streaming tests replace
only the external provider boundary. They are not evidence of model authoring
quality or a real external model call.

## Current evidence

- Final resource suite: **32 passed on SQLite** (19 HTTP cases and 13 unit
  cases); **19 HTTP cases passed on PostgreSQL 14.15**. Both run fresh migrations.
- **172 authoring/legacy/provider checks passed, one skipped** in the broader
  run. That combined run also initially reported three missing fixture imports
  in the new resource suite after lint cleanup. The imports were restored and
  the entire final resource suite reran successfully on both databases.
- Real create/edit tools, with supplied synthetic source and real browser render
  validation, created a collection-backed app without visualization IDs, saved a
  record, edited the UI, retained the record and rejected a stale UI edit.
  MCP create and exact-edit also passed through the same real validator.
  MCP schema management shares revision checks and permissions; nonowners are
  rejected before model context is built. Policy alternatives have a bounded,
  nonrecursive schema suitable for tool calling.
  This is tool-path evidence, not model-authoring evidence.
- SDK data alias/subscription, declared preview defaults/required fields,
  existing verification delivery and viewer hydration checks passed.
- Final production frontend build passed. Existing duplicate-key and dependency
  warnings remain; no separate TypeScript checker was available.
- Browser checks passed for saved records after reload, actual file upload,
  controlled-provider streaming, cancellation, explicit output saving/reload,
  read-only exploration and Hebrew RTL. A fresh anonymous browser saw published
  blog posts without drafts, private fields or editing controls.
- The same legacy artifact source rendered and its control worked on unchanged
  main and on this branch. Real analytical data/parameter preservation is covered
  separately by the legacy regression suite and SDK alias checks.
- Two API processes admitted exactly three of ten concurrent writes at a
  three-record quota, and both read the same three durable rows after restart.
- Local load smoke: 100 records (~14.3 MiB payload), 16 concurrent reads across
  two workers; median 563 ms, nearest-rank p95 1,076 ms, largest response 901,201
  bytes. Pages respected the 1 MiB bound and returned cursors. This is one local
  SQLite measurement, not a production throughput guarantee.
- A copied database plus encrypted storage restored 20 accessible records and
  four files through the resource services. Downgrade/upgrade on another copy
  preserved legacy UI versions; resource data was intentionally removed by
  downgrade, confirming that rollback needs the backup.
- Maintenance preserved live/recent blobs, removed an expired orphan, and its
  scheduler entry point ran successfully. The PDF subprocess accepted a valid
  PDF and rejected malformed input.
- Cancellation closes provider transports; token-limit truncation does not
  produce a completed result. Revoking report access or input-file permission interrupts streaming.
  Private provider logs are scoped per asynchronous context; concurrent ordinary
  diagnostics remain visible. HTTP tests check this across token waits. Real SDK
  calls to the localhost controlled provider also completed on both workers,
  with private input absent from their debug logs.
- **Real Luna/Sol evaluation now ran with explicit approval:** six full-completion
  API cases passed, plus four persistent generated apps were exercised. The fresh
  reading-list apps exposed a blocked-form compatibility bug, fixed in the
  platform and verified against the same generated source. See
  [the live completion report](2026-10-02-artifact-live-completions.md) for all
  outcomes, setup issues, warnings, screenshots and remaining limits.

Evidence lives under `media/pr/artifact-resources/`: `legacy-before-main.png`,
`legacy-after.png`, `record-persisted.png`, `file-upload.png`, `streaming.png`,
`summary-persisted.png`, `data-explorer.png`, `document-data.png`,
`data-explorer-he.png`, `analytics.png`, `public-blog.png` and `document-flow.webm`.
The streaming UI explicitly identifies controlled model output.

## Authoring regression found during qualification

The normal create tool still contained a second visualization-only guard after
its schema had been extended. A real call with supplied page source and a
collection returned `no_valid_visualizations` with `requested_ids: []`.
The guard now requires explicitly requested visualization/file references to
resolve; static and resource-backed pages may request neither. A permanent
regression covers both cases, and `tools/agent/artifact_resources/authoring.py`
proves create/edit behavior through the real browser validator.

## Implemented boundaries

- Artifact-scoped resources reuse existing report visibility, authenticated
  users, memberships and groups. Policies further restrict that entry boundary.
- Collection schemas are separate from rows; AI tools change definitions and
  generated UI uses the SDK for user content.
- Records are encrypted, equality indexes are keyed hashes, and mutation replies
  are encrypted acknowledgements. Optimistic revisions and idempotency protect
  writes. Retry acknowledgements have a seven-day horizon.
- Files use encrypted blobs on a configured shared volume, generated storage
  names, constrained media/size and authorized delivery. Public bytes require a
  readable record reference. Referenced files cannot be deleted.
- Streaming has actor and organization admission limits, a provider output cap,
  a connection deadline and periodic authorization checks even during stalls.
  Provider support requires an asynchronous bounded streaming implementation.
  Usage accounting is retained; output and execution status are not stored.
- Optional publication pins a completed UI version for existing shared routes.
  Legacy artifacts retain their version-list behavior until opted in.
- Views use expiring deduplication tokens, 30-day event retention and 400-day
  daily counters keyed by a pseudonymous authenticated viewer. Anonymous views
  do not claim unique people. Audit events omit user record/file content.

## Local deployment requirements

Artifact resources default to enabled. Organization admins can turn them off with
`enable_artifact_resources` in AI settings; the former environment flag is no longer used. Keep a stable
`BOW_ENCRYPTION_KEY` and configure `BOW_ARTIFACT_STORAGE` to a private shared
volume visible to every API worker. Database and blobs must be backed up together;
keep the encryption key separately. A local disk belonging to one worker is not
an acceptable multi-worker deployment. Index hashes depend on the same stable
key; replacing it without a data migration makes existing data unreadable.

`BOW_ARTIFACT_RESOURCES_READ_ONLY=true` disables resource writes and execution
while retaining authorized reads. Turning the whole feature off hides its APIs
and inspection controls; it does not delete resources or change sharing.

Defaults: 50 live definitions and 1,000 reserved resource identities per artifact; 10,000 rows/files per resource; 64 MiB per
resource unless configured lower/higher within its 256 MiB ceiling; 256 MiB total
artifact payload; 256 KiB records; 100 rows and 1 MiB per returned page; 10 MiB
uploads; 512 KiB JSON request bodies. Runtime requests have shared database
admission counters. AI permits 6 starts/minute per actor/artifact and 30 per
organization, with a 120-second connection limit and 4,096 output-token cap.
These bounds are not a throughput guarantee; load qualification remains required.

## Remaining release gates

Complete the repeated real-model authoring and runtime matrix, including independent
held-out attempts and browser interaction with model-generated apps. The initial
harness is `backend/tests/ai/test_artifact_resources_live.py`: ordinary prompts go
through the normal completion/planner path. It requires both
`ARTIFACT_LIVE_EVAL_APPROVED=true` and `OPENAI_API_KEY_TEST`. It does not embed
scenario recipes in system instructions or count hand-authored browser fixtures
as model successes. Complete the repeated/held-out protocol in the design plan
before enabling the feature.

Production capacity/failover qualification must use the intended database,
worker count, storage volume and ingress configuration. Local bounded-load
results above are evidence of behavior, not a substitute for that deployment
check. Apply migrations and deploy this code to **every** worker before enabling
the feature; old workers do not know the optional publication pointer.

The existing report DELETE endpoint archives rather than destroys a report.
Archiving retains records/files and suspends the new resource runtime. It does
not detach artifacts or purge archived data. Existing sharing settings are not
rewritten. Existing “Use this version” duplication creates another UI version
of the same artifact; it must preserve the same resource identity and data.
A future copy into a distinct artifact needs an explicit empty-data operation.

Run `tools/agent/artifact_resources/maintenance.py` from the backend environment
on the deployment scheduler. It removes expired acknowledgements/events/counters
and encrypted blobs without live bindings after a 24-hour grace period. Database
purges are batched; blob scanning is a maintenance-process operation.

Each API worker admits at most 16 concurrent ordinary resource requests by default
(`BOW_ARTIFACT_MAX_INFLIGHT`, 1–64). Excess requests fail before body buffering.
Streams use a separate pool of four connections (`BOW_ARTIFACT_MAX_STREAMS`, 1–16), so stream saturation does not consume ordinary request slots. This bounds upload/decryption memory alongside request-size limits. It is
transient transport backpressure, not stored execution state. Size worker memory
and shared storage for that configured concurrency; place normal ingress limits
in front of the service as for the existing API.

Organization-wide payload storage defaults to 1 GiB
(`BOW_ARTIFACT_ORG_MAX_BYTES`); each record/file owner defaults to 256 MiB across
that organization (`BOW_ARTIFACT_USER_MAX_BYTES`). Atomic counters also cap the
combined record/file count at 100,000 per organization and 25,000 per owner.
Collaborative edits charge the existing record owner, not the last editor.
Deletes release quota in the same transaction. These are payload budgets;
encryption, indexes, acknowledgements and audit metadata require additional disk.


## Reproducing the browser evidence

Use a disposable database and stable disposable encryption key. Run two API
workers on 8108/8109 with the feature enabled, storage configured, and
`TESTING=false` and `BOW_ARTIFACT_PREVIEW_URL=http://127.0.0.1:3118`. Run the frontend on 3118 with
`BOW_API_TARGET=http://127.0.0.1:8108`. Run the controlled provider with:

```sh
backend/.venv/bin/python -m uvicorn tools.agent.artifact_resources.controlled_provider:app --host 127.0.0.1 --port 8118
backend/.venv/bin/python tools/agent/seed_org.py --base-url http://127.0.0.1:8108 --email artifact-author@example.com --password 'Password123!' --org-name 'Artifact Sandbox' > /tmp/artifact-seed.json
backend/.venv/bin/python tools/agent/artifact_resources/seed.py
backend/.venv/bin/python tools/agent/artifact_resources/seed_document.py
backend/.venv/bin/python tools/agent/artifact_resources/seed_blog.py
backend/.venv/bin/python tools/agent/artifact_resources/seed_legacy.py
node tools/agent/artifact_resources/interactions.mjs
node tools/agent/artifact_resources/browser.mjs
node tools/agent/artifact_resources/public-blog.mjs
```

The baseline comparison script additionally expects unchanged main's frontend
on 3119, pointing at the same synthetic API. These fixtures are hand-authored and
synthetic. Their temporary authentication files stay outside the repository.

Publication selects UI versions on the existing shared-site routes. Existing
internal report access, code history and administrator visibility remain as on
main; publication is not a new confidentiality boundary for internal source code.
Resources independently enforce data/file permissions on every request.
