# Artifact apps: implementation and release plan

Status: implemented locally behind an opt-in flag; initial real Luna/Sol completion and browser checks passed, including a form-isolation regression fix. Full repeated release qualification remains incomplete. Updated 2026-10-02.
Branch: `codex/artifact-resources`, based on main at `c56f60c8`.
See `docs/feedback-loops/2026-10-02-artifact-resources.md` for measured evidence and outstanding gates.

This replaces the earlier direction in this document and supersedes `artifact-app-sdk-plan.md` and `artifact-app-sdk-pilot-plan.md`. Preserve those files as history. “App” and “site” describe what an artifact can do; they are not new database identities.

## 1. The approach

**Extend `Artifact` and `ArtifactVersion`. Keep the existing authoring tools and experience.** Attach persistent resources to the stable artifact ID, independently of its UI versions. No parallel App/AppRevision model, copying into a second object, or migration of all artifacts.

The user describes an outcome in chat: “Build a blog,” “Let customers upload documents,” or “Give these people editing access.” The agent configures resources, builds the UI, tests it, and explains the result. Users can inspect and manage everything through the artifact UI, but do not need to manually create tables or wire backend endpoints.

Generated UI uses a small SDK. Managed backend services enforce identity, permissions, persistence and execution. We initially generate frontend code and approved operation configuration, not arbitrary server code.

### First release

- Existing artifacts continue loading, editing, sharing and rendering their data/steps/visualizations.
- AI-led creation and modification using existing create/edit/read tools plus a focused resource-definition tool.
- Collections, records, validation, pagination and a read-only Data explorer.
- Private document uploads, blog cover images, authorized file delivery and deletion.
- Bounded LLM calls streamed over the active connection, with best-effort cancellation. Completed output can be explicitly saved as an ordinary record; no persisted execution state or recovery after disconnect.
- Existing identity, organization membership, groups and sharing unchanged; resource-local permissions for personal/shared data and published content within those sharing boundaries.
- Optional draft/published UI versions, existing sharing controls, basic views analytics and existing usage accounting.
- Isolation, quotas, migrations, backup/restore and deployment checks required to make these usable safely.

### Later, with extension points now

Custom domains, fully report-independent artifacts, application chat, report creation, durable execution/recovery, scheduled work, arbitrary backend functions, additional identity providers and multi-file authoring. These must fit existing artifact IDs, resource policies and operation contracts. Do not implement all of them in the first PR.

External sharing uses only the current product sharing paths. Establish their actual public-link and authenticated-recipient support on main before promising behavior. New guest identities, invitations, artifact roles or sharing semantics are out of scope. Anonymous writes/uploads/paid execution remain out of scope.

## 2. Reuse the existing model

`Artifact` already provides stable identity; `ArtifactVersion` provides UI history. Both currently require a report. Keep that relationship initially. A thin artifact access resolver delegates to existing report/artifact sharing checks without changing them. Resource permission checks then further restrict what a permitted viewer can do; they cannot grant entry to the artifact or access to its report conversation or private sources.

| Existing or new piece | Implementation |
| --- | --- |
| `Artifact` — extend | Preserve ID, organization, report, mode, title and history. Add optional runtime/settings metadata and a published-version pointer, or a one-to-one settings row if that keeps legacy reads simpler. |
| `ArtifactVersion` — extend | Store SDK contract version and resource requirements alongside existing UI content. Preserve current version creation/edit contracts. Published source is immutable; generated metadata can still follow existing rules. |
| Resource definition — new | Artifact ID, stable resource ID, name, kind, schema/configuration, policy and revision. Collections, file resources and operation definitions use the same ownership convention; use typed validators per kind. |
| Records — new | Artifact/collection, subject, payload, record revision and timestamps. Collections are logical tables stored in shared backend tables initially. |
| File bindings — extend/add | Bind existing file identity to artifact/resource/subject, readiness and access scope. Do not automatically attach an upload to the report. |
| Sharing — unchanged | Reuse current users, groups, shares, public links and access checks. No new grants table, guest flow or artifact role system in this release. |
| Analytics — new | Minimal view events and daily aggregates keyed by artifact ID. Keep audit and operational usage separate. |

Schema/configuration and permissions live together on the resource definition, with a revision checked on updates. Existing user/group IDs are references, not copied memberships. Every resource belongs to an organization and artifact, not a UI version. Every lookup checks those boundaries. Stable IDs survive renames; deleting/recreating a resource name must not expose the previous resource's data.

Keep one UI source entry for now. A versioned manifest can later describe multiple assets without changing identity or storage. Do not make a project editor or a separate database per artifact prerequisites.

### Publication without breaking legacy behavior

Existing artifacts continue their current newest-version behavior. New resource-backed sites can opt into explicit publication: edits create drafts, and publishing atomically selects a compatible existing `ArtifactVersion`. Enabling this mode on an existing artifact must deliberately preserve its currently visible version.

Publishing UI and publishing blog content are distinct. A post becoming public is a record transition checked by backend policy. Reverting UI does not restore permissions, remove records or change which posts are published.

Resource policy never comes from whichever UI version happens to be latest. Declarations in code are requirements, not permission grants.

## 3. AI authoring: define resources, then build the UI

Keep three responsibilities clear:

| Layer | Does | Example |
| --- | --- | --- |
| Resource authoring | Defines collections, schemas, validation, indexes and resource access rules. Defines allowed file and backend-operation capabilities. | Create `blog_posts`; add a category field; require a unique slug. |
| UI authoring | Builds and edits the interface against those definitions. | Blog list/detail, editor form, upload button and streaming progress. |
| Runtime SDK / generated artifact UI | Reads/writes actual records, transfers files and invokes approved operations as the current user. | Save a post, upload a cover, summarize a document. |
| Data explorer | Read-only inspection of permitted collections, schemas and records. | Browse Blog posts and inspect a record. |

The resource-authoring tool does **not** create or edit individual blog entries. Updating a collection means changing its definition; updating a post means a record mutation through the runtime API. Sample rows used for verification are fixtures, not production content.

### Small tool surface

| Tool | Responsibility |
| --- | --- |
| `create_artifact` | Preserve current behavior and result IDs. Accept optional resource definitions/requirements at creation so the user gets a complete artifact from one request. |
| `edit_artifact` | Preserve mechanical code edits and visualization continuity. Accept optional requirement changes and expected-version checks. Does not silently change schemas or permissions. |
| `read_artifact` — extend | Return code/version and bounded resource definitions, bindings, effective permissions and schema revisions. No private records or secrets by default. |
| `manage_artifact_resources` — new | Explicit `create`, `update`, `delete` actions on collection, file or approved operation definitions. Updates include an expected revision; deletion reports data/dependency impact. This replaces the broad `configure_artifact_resources` proposal. |
| Existing sharing operation — unchanged | Use supported product sharing actions as they are. Resource authoring never creates a new audience or bypasses artifact visibility. |
| `publish_artifact` — for opted-in publication | Validate requirements and select the UI version through the same service as the UI. |

Resource rules reference existing users, groups and permission conventions. The resource tool updates schema/configuration and permissions together on that resource. Sharing controls who can open the artifact; resource rules control what those viewers can read/change. Both checks must pass. The server validates references and the caller's authority; there are no new artifact roles or group memberships.

A resource change has a small typed contract: artifact ID, action, kind, resource ID for update/delete, definition/change fields, expected revision and idempotency key. Return stable IDs, current revision, applied/proposed diff and actionable errors. Do not accept arbitrary SQL, server code or an opaque configuration blob. No separate create/update/delete tool is needed for each resource kind.

Example: `create collection blog_posts` defines fields, defaults and uniqueness. `update blog_posts` adds an optional category field. `delete blog_posts` identifies affected data and bindings and requires an authorized deletion/migration path; it does not silently cascade from a UI edit.

### Creation and repair flow

1. Read current artifact context and supported SDK/resource contracts. Keep documentation short, general and available to every authoring session.
2. For a new artifact, creation stages identity, UI and optional resource definitions together. Validate using fixtures, then commit approved definitions and the artifact. Existing calls without resources retain their current path.
3. For an existing artifact, inspect before changing definitions, then edit UI against returned resource IDs. Both creation and resource management use one backend validation/policy service. Do not hold a database transaction open during model generation or browser verification; validate against expected revisions and commit with a final concurrency check.
4. Exercise intended interactions using fixtures and return structured errors for repair. If editing fails, preserve the working version and report any unused new resources. Retry idempotently; never automatically delete resources containing user data.
5. Show the working preview and a concise schema/access summary. Ask only for missing decisions or authorization, using a concrete diff; do not repeatedly confirm already authorized work.

An artifact without analytical visualizations is valid when backed by records/files or static content. A user should not need to know tool names, schemas or backend terminology to create one.

### Example: “Build a blog”

The agent defines Blog posts with suitable fields and a file resource, then builds a working list/detail view and permitted authoring controls. Existing groups can be selected for editing/publishing. The user can inspect **Data → Blog posts** and its schema. Real posts are subsequently entered through the generated site using the records SDK. The Data explorer only displays permitted schemas and records.

Follow-ups such as “add categories,” “make published posts public,” and “let the content team edit” modify the same artifact. Missing audience or group identity can require a brief clarification; the agent must not invent a group or assume public access. Instructions in files or records are untrusted content, not permission to change configuration.

## 4. Existing identity and sharing; permissions on resources

Reuse existing authentication, users, organization membership, groups and sharing. The model entry points are `backend/app/models/user.py`, `backend/app/models/membership.py`, `backend/app/models/group.py` and `backend/app/models/group_membership.py`; reconcile their services against implementation main. Do not build another directory, guest flow or sharing system.

Authorization has two checks:

1. **Existing sharing:** can this visitor open this artifact through the current product access path?
2. **Resource permissions:** may that viewer read/change this record, access this file or invoke this operation?

A resource permission cannot widen artifact visibility. Conversely, sharing a page does not expose all its records, files or analytical sources. Existing source permissions remain authoritative.

| Concept | Source and use |
| --- | --- |
| Identity | Existing authentication or the existing public-view context. Server resolves the actor; no creator fallback. |
| Organization role | Existing membership/permission checks, unchanged. |
| Groups | Existing server-resolved memberships, referenced by stable ID. |
| Sharing | Current product sharing flow and semantics, unchanged. |
| Resource permissions | Stored beside the resource schema/configuration; define operations, ownership, allowed fields and row conditions. |

UI receives only needed identity/display capabilities. Never trust actor IDs, group names, roles or ownership fields from generated code. Recheck membership/access on calls and invalidate relevant caches when access changes. Recheck active LLM streams within a bounded interval, targeted at 30 seconds, and stop delivery/cancel best-effort when access is revoked.

### Blog posts definition

Store schema and permissions together, conceptually:

```text
Blog posts
  schema: title, unique slug, body, status, cover_image
  permissions:
    read: published rows for viewers allowed by existing sharing
    create/update drafts: existing Content Editors group
    publish/unpublish: existing Publishers group
```

The names illustrate existing groups selected by ID, not groups automatically created by the feature. If a requested group is missing or ambiguous, clarify. Define draft reads and field restrictions explicitly too. Personal collections default to subject-owned access; permission to manage schema/UI does not automatically permit reading everyone's personal records.

Use a small declarative policy vocabulary with server-enforced operations, user/group references, ownership, fields and typed row predicates. Apply it to list/get, mutations, status transitions, references and file delivery. A client-side published filter or hidden button is not a permission check. Code cannot replace policy with arbitrary executable rules.

Public blog records require both a supported existing public share and an explicit resource read policy. Cover files additionally require an authorized published-post reference. Unpublishing denies new public body/cover reads; reauthorize delivery and use conservative cache headers. Already downloaded content cannot be recalled. Default new resources to private until an authorized policy is applied.

### External sharing boundary

Test existing external sharing exactly as supported. Do not promise invitation-only editors or verified guest accounts unless they already work under current identity/sharing. Unsupported asks must produce an honest explanation, not a newly invented sharing mechanism or a substituted public link. Public readers cannot write, upload or invoke paid operations. Keep broader external collaboration as separately scoped future work if the baseline lacks it.

## 5. Runtime, SDK and implementation seams

Use modules such as `context`, `data`, `records`, `files` and `ai`. Existing helpers for artifact data, query parameters, steps, visualizations, themes and viewer display delegate to the same authorized implementation. Preserve supported historical code through adapters.

```text
Generated artifact UI
  → versioned SDK / trusted artifact host
  → authenticated artifact-scoped API
  → access resolver + resource policy + quotas
  → records / files / data / operation services
```

Proposed API family: `/api/artifacts/{artifact_id}/resources`, `/collections/{collection_id}/records`, `/files`, `/ai/stream`, `/publish` and `/analytics`. These are logical routes to reconcile with the existing router, not parallel implementations of legacy artifact services. Requests have schema versions, request IDs, bounded inputs and explicit error codes. Record/resource mutation retries use idempotency keys; conflicting edits use expected revisions. LLM calls are not automatically retried or durably deduplicated.

| Existing seam | Planned changes |
| --- | --- |
| `backend/app/models/artifact.py`, `backend/app/services/artifact_service.py` | Extend settings/publication and version requirements; preserve constructors, history and identity. |
| `backend/app/ai/tools/schemas/create_artifact.py`, `backend/app/ai/tools/schemas/edit_artifact.py` | Add backward-compatible optional fields and resource/verification results. |
| `backend/app/ai/tools/implementations/create_artifact.py`, `backend/app/ai/tools/implementations/edit_artifact.py` | Use shared resource validation and fixture verification; preserve current exact-edit behavior. Update all tool exposure paths consistently. |
| `frontend/components/dashboard/ArtifactFrame.vue`, `frontend/utils/artifactIframe.ts`, `frontend/public/libs/artifact-globals.js` | SDK bootstrap, safe transport, identity changes, streams and legacy adapters across embedded/shared/full-screen views. |
| `backend/app/services/file_service.py`, `backend/app/services/file_access_service.py` | Storage interface, artifact bindings, upload lifecycle and scoped delivery. |
| `backend/app/services/llm_usage_recorder.py` | Preserve existing usage accounting; add artifact/request/actor attribution where supported without retaining execution output or status. Admission limits belong before calls. |

New backend responsibilities: resource validation/access, records service, a request-scoped LLM streaming handler and view-event ingestion. Reuse existing identity/sharing services unchanged. Keep them in the existing service architecture. Inspect current implementations on refreshed main before deciding exact filenames or reuse.

### SDK v1: concrete contract

The following is the proposed public contract, not an implemented library. Expose one `bow` namespace with typed methods and a short authoring reference. Keep existing globals available unchanged. Small UI hooks wrap core methods for loading/error/progress state; they do not implement separate networking or authorization. Resource schema authoring stays in tools, outside this runtime SDK.

| Module | Proposed surface | Result / behavior |
| --- | --- | --- |
| `context` | `get()`, `subscribe(listener)` | Snapshot of artifact/version, viewer display identity, execution mode and server-derived capabilities; subscription returns unsubscribe. No credentials. |
| `data` | `getSnapshot()`, `subscribe(listener)`, `vizById(id)`; aliases for `useArtifactData`, `useParams`, `useParamOptions`, `useFilters` | The existing data payload and helper contracts, backed by the same stores. No new query engine. |
| `records` | `collection(nameOrId).list/get/create/update/delete` | Validated records and cursor pages from that declared collection. |
| `files` | `upload(resource, file, options)`, `get(fileId, options)`, `download(fileId, options)`, `delete(fileId, options)` | Ready file reference, authorized metadata/content or deleted-binding acknowledgement. |
| `ai` | `stream(operation, input, { signal })` | Async iterator of text deltas and one completion event; typed errors reject iteration. No saved execution, resume or history API. |

The host binds artifact/organization and authenticates the request. Names resolve to stable resource IDs within the current artifact; no method accepts an arbitrary acting user or group. Renaming a resource preserves its ID; keep existing declared aliases until dependent UI versions migrate, or reject incompatible publication. Do not resolve an old alias to a new resource with different data.

#### Records

```js
const posts = bow.records.collection("blog_posts");
const page = await posts.list({
  filter: { status: "published" },
  orderBy: [{ field: "createdAt", direction: "desc" }],
  limit: 20,
  cursor: nextCursor,
  signal
});
// page: { items: Record[], nextCursor: string | null }
// Record: { id, data, revision, createdAt, updatedAt }

await posts.update(post.id, { title: "Updated title" }, {
  expectedRevision: post.revision,
  idempotencyKey: editKey,
  signal
});
```

`get(id, { signal })` returns one record. `create(data, { idempotencyKey, signal })` returns the created record. `update` accepts a partial field patch; omitted fields remain unchanged and null is validated against the field schema. `delete(id, { expectedRevision, idempotencyKey, signal })` returns `{ id, deleted: true }`. System IDs, ownership and timestamps are server-assigned; ownership is not exposed unless allowed.

Define v1 filters as typed equality conditions combined with AND; only declared indexed fields are accepted. `orderBy` also uses allowed indexed fields. Add the stable ID as a tie-breaker, use opaque cursors and enforce row/byte limits server-side. Reject unsupported filters instead of silently scanning or partially applying them. The sample published filter never substitutes for backend read policy. Handle not-found/denied responses without leaking whether another user's record exists.

Each logical mutation gets a stable idempotency key, retained for retry of that mutation only; the SDK may generate it but must make it reusable after an ambiguous response. Reusing a key with different input conflicts. Aborting an HTTP request does not prove a mutation was rolled back. Preserve typed input on optimistic conflicts and re-fetch before resolving; do not automatically overwrite newer data.

#### Files and connected AI

```js
const file = await bow.files.upload("documents", selectedFile, {
  onProgress: ({ loaded, total }) => showProgress(loaded, total),
  signal
});
// file: { id, resourceId, name, mediaType, size, status: "ready" }

let text = "";
for await (const event of bow.ai.stream("summarize_document", {
  fileId: file.id
}, { signal })) {
  if (event.type === "text_delta") text += event.text;
  if (event.type === "completed") showResult(event.output);
}
// Saving output, if requested, is a separate records.create call.
```

Upload progress measures transferred bytes, not successful validation; show a finalizing state until a ready reference is returned. Failures use typed errors; abort triggers best-effort cleanup of pending uploads. `get` returns authorized metadata. `download` returns an authorized Blob for bounded supported files; UI helpers manage temporary object URLs and revoke them on disposal. `delete` removes the artifact binding using a mutation idempotency key, subject to references/retention. Never return storage paths or permanent public credentials/URLs. Existing embedded file components keep their own compatible adapter.

AI accepts only a declared operation and schema-valid input. A `text_delta` contains appended text; a `completed` event contains the authoritative final output matching the operation output schema. Structured operations may stream text progress but their final structured output must validate. Exactly one completion event is allowed on success. A stream ending without completion is an `INTERRUPTED` error, not success. Errors reject the iterator; do not also yield duplicate error events. Partial text can stay visible as incomplete within the current page.

`AbortSignal` supports cancellation of uploads, reads and active model calls. Abort is best-effort upstream and may still incur usage. AI has no automatic retries or durable deduplication. Reload loses active/unsaved output. Repeated UI rendering or hook mounting must never start a paid call; start only on an explicit user action.

#### Errors, lifecycle and versions

New SDK errors use `{ code, message, requestId, details? }`, with safe bounded details. Codes cover `UNAUTHENTICATED`, `FORBIDDEN`, `NOT_FOUND`, `VALIDATION`, `CONFLICT`, `QUOTA_EXCEEDED`, `RATE_LIMITED`, `UNAVAILABLE`, `ABORTED`, `INTERRUPTED` and `UNSUPPORTED_VERSION`. Keep existing data-helper error shapes compatible through adapters. Do not expose backend traces, secrets or denied payloads.

Subscriptions return cleanup functions; UI hooks unsubscribe on unmount. Changing artifact, viewer or mode clears scoped caches and closes old channels; logout stops delivery and aborts active requests best-effort. Keep capabilities current, but treat them as display guidance: the server always enforces policy. Fixture mode uses identical signatures and deterministic streams selected by the trusted host, never a caller-controlled live/test flag.

Store a separate SDK contract version on new artifact versions, without repurposing the existing visual runtime version. Older artifacts with no SDK metadata use their existing helpers. Version the host and runtime together, validate compatibility at preview/publication, retain supported old contracts, and return a clear unsupported-version state rather than silently reinterpreting calls. Publish types and general examples from the same contract used by service validation; include them in Luna's ordinary reference, not scenario-specific prompts.

### Data compatibility: an additive facade, not a rewrite

Full compatibility with supported existing data behavior is a release requirement. The inspected checkout uses `ARTIFACT_DATA`, `useArtifactData`, `vizById`, `useParams`, `useParamOptions`, `useFilters` and `useCurrentUser`. Reconcile main before implementation and inventory additional host/backend producers and consumers. Preserve existing artifact code and stored IDs without requiring users to regenerate artifacts.

Implement `bow.data` over the existing data/parameter/filter stores. Both old globals and new methods read the same snapshot and use the same host query/refresh path. Do not introduce a second cache, transform analytical rows into collection records, or change payloads to fit the new records API.

| Existing contract | Required preservation |
| --- | --- |
| `ARTIFACT_DATA` / `useArtifactData()` | Preserve payload fields, optional/null behavior, initialization timing and live notifications. Keep report, visualization, file, viewer, parameter and runtime metadata; do not drop provenance or verification metadata. |
| `vizById(id)` | Return the existing visualization object, compare IDs with existing string-equivalence behavior, return null when missing, and preserve visualization ordering/IDs and row/column metadata. |
| Query/step bindings | Keep existing query/step references, data types, nulls, row shapes, visualization configuration and source permissions. IDs alone never authorize fetching arbitrary steps. |
| `useParams()` | Preserve `declarations`, `values`, `pending`, `loading`, `error`, `setParam`, `setParams`, `apply`, `refresh`, `getOptions` and existing signatures. Preserve default debounce, `apply: false` staging and refresh/apply target semantics. |
| Parameter delivery | Preserve pending/in-flight values, sequence acknowledgements, dependency ordering, and stale-response handling. A delayed host response must not erase newer user input. Data arrival after a failure must not silently clear its error. |
| `useParamOptions(name)` | Preserve query-backed/static options, value-label normalization and null/empty fallback behavior. |
| `useFilters()` | Preserve local filtering/reset semantics and keep local filters distinct from server query parameters. |
| Identity and viewer display | Preserve existing `current_user` / helper conventions and anonymous defaults. Server-bound identity parameters cannot be overridden by new SDK calls. View-as remains a non-writing inspection context. |
| Files, provenance and exports | Keep existing embedded file helpers, visualization information, export data and permitted static/offline behavior. Never add live writes/model calls to exports. |

The current parameter store uses a 250 ms debounce; retain it unless changed as a separately tested product decision. Existing helpers keep their present synchronous return contracts; do not turn legacy `apply`/`refresh` into promises or infer completion from message dispatch. New `getSnapshot()` reads current state; `subscribe` signals changes and returns an unsubscribe function. New hook aliases preserve existing hook semantics rather than changing reactivity under old names.

Use one compatibility corpus across in-app, shared, full-screen, preview/verifier, external-host and static/offline render paths supported by the product. Compare payloads, helper results, query requests and interaction outcomes before/after the adapter. Cover empty/partial/error data, fast parameter changes, out-of-order responses, refresh targets, dynamic options, multiple viewers and source revocation. Existing offline exports retain their supported snapshot behavior; new network-only capabilities show unavailable offline instead of silently writing or failing without explanation.

Add a mixed old/new test: one artifact uses existing data helpers for a chart and the new records SDK for notes. Parameter refresh updates the chart without resetting notes or reading another user's cache. Both data entry points observe the same authorized snapshot. Block rollout on unexplained differences; isolation changes may replace transport internals, never silently drop supported data behavior.

### Isolation is a prerequisite

Generated code must not read privileged browser storage, parent DOM or general platform credentials. Use a separately isolated origin or a proven opaque-origin sandbox, with a per-document nonce/channel handshake. Invalidate channels before navigation, revision replacement and logout; window identity alone is insufficient. Validate messages, bound buffers and restrict network access.

Protect against ambient-cookie requests as well as direct token access. Check all legacy renderer paths: an unsafe old frame in the same authenticated session would undermine the boundary. Preserve helpers with safe adapters; do not fall back to a privileged frame when compatibility fails.

The SDK contract is transport-independent. Embedded hosting can use the trusted host bridge; a future custom domain can use app-scoped authentication and the same service contracts. No runtime service assumes a parent report URL or cookie domain.

## 6. Records, files and connected LLM calls

### Records

Use shared tables for collection definitions and records, with artifact/organization scoping and suitable indexes. Reuse supported encryption primitives for payloads; document what query indexes expose. Do not decrypt and scan whole collections for filtering.

Validate types, required fields, defaults, references and field permissions. Support deterministic cursor pagination, indexed filters and optimistic updates. Enforce declared uniqueness such as blog slugs with a transactional index/constraint strategy, including concurrent requests. Start with indexed system fields and a limited set of declared typed fields. Reject unsupported queries explicitly.

Apply record/byte quotas atomically, with reconciliation after failures. Proposed defaults: 256 KiB/record, 50 rows/page, maximum 100 rows and 1 MiB/response, 10,000 records/collection. Validate aggregate and per-subject storage caps during load testing. Existing over-limit data remains readable, not truncated.

Additive fields must preserve existing records. Destructive/type changes require a concrete migration preview and recoverable execution; reject unsupported migrations initially. UI rollback never rolls back data or policies.

### Files

Reuse file identity with artifact-specific bindings. Add storage operations for begin/write/finalize/read/delete, opaque keys and persistent metadata. First-release production uploads require a supported persistent shared-storage configuration accessible to all workers; local development uses a local adapter. Prove restart and multi-worker durability.

Upload lifecycle: authorize/reserve quota → pending upload → transfer progress → validate bytes/type → finalize ready file/binding. Expire abandoned uploads and release reservations idempotently. Only ready files can be downloaded or processed. Binding deletion must preserve bytes still referenced elsewhere.

Support text/PDF analysis inputs and validated raster images for blog covers. Proposed starting limits: 10 MiB/file, 50 PDF pages, bounded extracted characters and image pixel dimensions. Reject unsupported/encrypted/malformed documents and active image formats initially. Strip unnecessary image metadata and serve validated images with appropriate content headers. Parsers run with bounded memory/time and no ambient credentials/network.

Private downloads reauthorize the caller. Public cover delivery follows the published-record policy. Do not expose storage paths, credentials or permanent unrevocable public URLs. Derived text and LLM results inherit input scope; saving them to a broader audience requires an explicit authorized disclosure.

### LLM calls and streaming: no persisted execution state

Store the operation **definition** on the artifact resource: approved model/configuration, input/output schema, allowed resources, output scope and limits. Do not store individual executions, progress snapshots, leases, result history or job status. No run table, background queue, polling API or Activity/history UI in release one.

On an explicit user action, the backend authorizes the viewer and resource, validates input, invokes the approved model and streams to the active connection. Use request-local memory for streaming and cancellation. Small document extraction also runs within bounded requests; extracted content is not a durable job checkpoint. No arbitrary server code or unrestricted tools.

The SDK exposes a stream with incremental text, completion/error and an abort control. Disable repeat submission while a call is active and never automatically retry paid requests. This is not a global exactly-once guarantee: another tab or an explicit retry can start another billed call.

On abort, disconnect or timeout, attempt upstream cancellation, release transient capacity and stop delivery. Cancellation may not prevent all provider usage. Reload and worker restart lose the active call and unsaved output; there is no resume, replay or recovery promise. A new attempt is explicit and may incur another charge. The UI explains interruption without claiming completion or successful cancellation when uncertain.

If the user wants to retain a completed result, save it explicitly through the ordinary records API after output validation. Recheck destination permission and scope. A successful save survives reload because it is user data, not execution history. If saving fails, retain output only in the current page's memory for a save retry; after reload unsaved output may be lost. An ambiguous record-save response is safely retried with the same record-mutation idempotency key, without re-invoking the model. Do not persist hidden partial-result buffers in browser storage as a recovery substitute.

Start with a configurable 120-second request deadline, token/output bounds, bounded stream buffers and existing admission/rate-limiting infrastructure. Support deployment-wide capacity controls where available; transient counters are not execution records. Confirm supported multi-instance limits before rollout rather than silently relying on per-process limits.

Keep existing model/token/cost usage accounting separate from execution state. Request IDs may correlate usage/errors, but must not become a result/status recovery API. Do not claim hard currency caps from after-call accounting. Use current model/source permissions and never borrow the creator's credentials.

## 7. Artifact UI: Data explorer and Analytics only

Add two basic views to the existing artifact experience. Do not introduce a separate Apps hierarchy, a new Files manager, a permission editor or an execution/history view. Existing sharing and authoring controls remain in place. Resource schemas and permissions are configured through AI authoring tools; generated artifact UI uses the SDK for permitted record/file mutations.

| View | First-release scope |
| --- | --- |
| Data explorer | Select a collection such as Blog posts, inspect its schema, browse a paginated table, use supported filters/sorting, refresh and open read-only record details. |
| Analytics | Total views, daily views, authenticated unique viewers, a date range and basic embedded/standalone breakdown. Distinguish anonymous visits from unique people. |

The explorer has no add/edit/delete/import/bulk-action controls, editable cells or schema/policy editing. It uses existing collection-definition and record read APIs, with bounded indexed pagination. File fields show safe references/metadata; authorized previews may reuse existing viewers, without adding file management. Opening or refreshing either view performs no mutations or model calls.

Use current authorization for who may inspect an artifact's management views, then apply resource row/field permissions independently. Being an artifact author/manager does not automatically reveal personal records. Analytics aggregates also require authorized access and must not expose raw identities. Include loading, empty, denied, expired-session and unavailable states; follow existing locales and layout direction. Keep this first UI small and read-only.

Preview/render verification continues to use isolated fixture resources and fake streams. View-as remains read-only simulation. Explicit live testing uses the tester's real identity and cannot be enabled by changing a client flag. Page load or screenshot capture never triggers paid operations or live mutations automatically.

### Deferred: querying artifact collections through create_data

Later, make permitted artifact collections discoverable/queryable through `create_data`, analogous to the existing `bow.runs` analytical source. This is not an execution-history feature or a new runtime SDK namespace. It is **not part of this release**: no collection registration in the analytical catalog, query adapter or automatic chart generation from artifact records now.

Keep stable collection IDs, typed schemas and reusable server policy checks so that integration can be added later. A future query adapter must enforce the same artifact access, row and field permissions; direct internal database access must not bypass them. System view analytics and user-defined records remain distinct datasets. Existing `bow.data`/legacy analytical helpers remain fully compatible; the explorer uses records read APIs and does not require this future integration.

### Views

The trusted host records a view after a published site becomes visible and initializes. Count once per host navigation with a server-issued deduplication token; reconnect does not add a view, reload does. Exclude previews, verifiers, exports and view-as.

Store artifact/version, time, surface and minimal pseudonymous subject/session information where permitted. Do not store record contents, prompts, filenames, full URLs or sensitive referrers. Anonymous sessions are not unique people; avoid fingerprinting.

Validate/rate-limit ingestion and deduplicate aggregation. Analytics failure must not block rendering. Proposed retention: 30 days of events and 13 months of daily aggregates, configurable. Separate product views from operational usage and security audits. Audit permission/schema/publication changes and mutations without copying sensitive payloads.

## 8. Compatibility, lifecycle and future independence

Implement on refreshed main, not the current planning branch or an earlier records branch. Reuse validated pieces from PR #1208 selectively; inventory whether any installation has deployed its records before choosing migration steps. Use additive migrations and preserve IDs, payloads, authorship and effective permissions. No eager artifact conversion.

Centralize artifact authorization now. Keep report ownership initially; existing report deletion semantics must be made explicit for attached resource-bearing artifacts, with a resource-impact summary before deletion and reference-aware cleanup. A future detach operation will establish independent ownership/access and audit all source dependencies before making report associations nullable. Do not silently detach on report deletion or promise survival before that lifecycle exists.

Source bindings retain their own authorization even when an artifact is separately shared. Cache keys include principal/access scope, artifact, binding, parameters and relevant revisions. Never reuse the creator's private result cache for external or public viewers.

Duplication copies UI and compatible definitions with empty records/files; follow existing sharing/duplication behavior without silently widening resource permissions. Unpublishing denies new public access; resource data remains. Deletion denies new calls, cancels active requests best-effort and purges under retention/reference rules. Resource writes/downloads recheck deletion/access state. Creator departure leaves organization control intact without automatically exposing personal payloads.

Feature flags independently control new authoring and runtime resources; existing sharing remains unchanged. Rollback disables new writes/execution while keeping authorized data readable; it is not a policy rollback or destructive schema downgrade. Verify backups/restore of database and file bytes together and mixed-version deployment behavior.

Future additions use the same foundation:

- Custom domains map verified hosts to artifact IDs and published versions, with domain-aware authentication.
- Chat can later store artifact-owned conversations and invoke model handlers under the current principal; conversation/execution persistence is not part of this release.
- Report creation is a permission-checked handler with explicit destination/audience and idempotency.
- Custom functions can later use isolated workers behind the same resource clients and limits. Durable execution/recovery would require an explicit future design, not hidden state added now.
- Detached artifacts reuse their IDs and resource ownership; only report coupling and access resolution evolve.

## 9. Realistic end-to-end acceptance cases

These cases describe expected behavior for evaluators; do not paste their implementation steps into the authoring prompt. Use Luna for authoring evaluations under the protocol in section 10, then exercise the result through real service authorization and persistence. Record artifact/version IDs and evidence. Controlled runtime model streams test failures deterministically; they do not replace actual Luna authoring or the real runtime LLM smoke test.

### A. Legacy artifact loads and edits correctly

Open an existing dashboard with parameters, steps, charts and file references as its owner, another authorized viewer and through its existing shared link. Test filters, refresh, full screen, themes and export. Ask “add a notes section” and verify existing charts/IDs/history remain intact. Existing create/edit calls need no new fields; no external viewer receives private source data; verification produces no live side effects.

### B. Create a document analysis artifact

Example user ask: “I need a page where I can upload a document and get a summary. I want to see it working and come back to the result later.” The agent configures private files, a bounded analysis operation and a personal results collection, then builds and verifies the UI. Upload a real supported file; observe upload progress and incremental model output; wait for completion, explicitly save the summary and reload to read that saved record. Separately reload during streaming: the UI offers a new attempt, with no recovery promise or automatic model call. Another user cannot read the file or summary. Test invalid file, quota failure, cancellation and expired login.

### C. Create a blog site through chat

Example user ask: “Make a blog for our team with pictures. We should be able to work on drafts before readers see them.” The agent creates Blog posts and cover resources, list/detail pages and editing controls. Author a draft, upload a cover, publish it, then visit its stable post URL anonymously in a fresh browser. Refresh/deep linking works. Draft body, internal fields and unpublished cover are denied even by direct ID. Concurrent attempts to create the same slug produce a useful conflict. Public rich content is sanitized and cannot execute scripts. Post publication is independent of UI publication.

### D. Add permissions to the blog

Example follow-up: “Let our content team write posts, but only the publishing team should make them public.” Use real existing test groups or allow a clarification if the requested group is ambiguous. The agent reads current policy, presents/applies the authorized diff and preserves posts. Test as reader, editor, publisher and manager, including direct API requests. An editor cannot set published status through a generic record update or change access. A stale policy change conflicts. Remove a user from the editing group while signed in: subsequent edits fail. Forged client group membership is ignored. Reverting UI cannot restore the old permissions.

### E. Share with external people using current sharing

Use the existing share flow to open the blog in a fresh external browser. For supported public links, published rows and approved covers are visible while drafts/private sources and writes remain denied. Test existing authentication requirements, expiry/revocation where supported and denied viewers. Ask for an external editor: either demonstrate that exact permission through existing supported sharing or explain the limitation. Do not silently make the site public, create new guest identities or widen report access to satisfy the request.

### F. Evolve a populated site without rebuilding it

Example follow-up: “Can we group posts by topic and show a short description?” The agent reads schema/version, applies an additive change and edits the same artifact. Existing posts, URLs, images and existing sharing survive. Retry the request after an interrupted tool response: no duplicate collection, field or version mutation. A subsequent request to remove populated fields produces a concrete migration impact, not silent data loss.

### G. Diagnose and repair a broken interaction through chat

Introduce a schema mismatch in a draft UI. The agent gets a structured verification error, repairs the field reference and verifies again. The previous published UI stays live; there are no real uploads, records, sharing changes or model charges from verification. If repair fails, report the exact failure and any unused configured resources; do not claim a completed app.

### H. Read-only explorer matches runtime permissions

Open Data explorer on a collection large enough for multiple pages. Select the collection, inspect its schema, filter/sort, open record details and refresh after a permitted change made in the generated site. Verify that no record/schema editing, deletion, upload or import action appears and that exploration sends no mutation requests. An artifact manager cannot inspect another person's private results without resource permission; denied fields and rows stay hidden even through direct read requests. Empty/denied/error states are understandable. Separately test concurrent record edits through the generated site/SDK for conflict handling without lost input; this does not require adding editing controls to the explorer.

### I. Interrupted streaming and explicit retry

Start a call and verify repeat submission is disabled on that page. Separately abort, disconnect, reload and terminate the serving worker. There is no persisted execution to recover and no automatic paid retry; the UI reports interruption and offers an explicit new attempt where appropriate. Cancellation is best-effort and incurred usage remains recorded. After a completed call, make record saving fail: retry from page memory without regenerating. Reload before a successful save: no promise that output survives. Retry an ambiguous record-save response using the same mutation key and verify one saved record.

### J. Unpublish, revoke and inspect analytics

Open the blog through supported sharing as different allowed viewers; inspect the Analytics view and verify total/daily views, authenticated unique viewers, date filtering and surface breakdown. Check empty ranges and denied analytics access. Preview and run a verifier: these do not inflate views. Unpublish a post and revoke access through existing sharing/group controls; new body/cover reads fail and active stream delivery stops within the bound. Public responses do not keep serving revoked content from an avoidable application cache. Analytics failures do not break the site.

### K. Preserve analytical data while adding records

Example follow-up: “Can the team leave notes on this chart?” The agent preserves visualization and parameter bindings, adds a comments collection and uses stable source keys. Two members share comments while personal preferences remain separate. Sharing the artifact does not grant access to the sales query. Source permission revocation invalidates refreshes and caches.

### L. Resource limits, restore and lifecycle

Race uploads/record writes/stream requests at quota limits across two instances. Verify atomic admission, bounded pages and cleanup of abandoned reservations. Restart services and restore a backup: file bytes and metadata still match. Duplicate the artifact: it has empty data and follows existing sharing behavior without broadening resource access. Delete the containing report through its declared lifecycle: show resource impact and perform correct retention/cleanup, with no accidentally orphaned public endpoint.

Also verify malicious cross-artifact IDs, forged identity fields, reused channels after navigation, prompt injection in documents/posts, invalid model output, unauthorized file references and attempts to publish private derived data.

## 10. Evaluate realistically with Luna

**Luna is the authoring evaluation model.** Record the exact deployed model identifier/version, settings and token/tool/time budgets in the evaluation manifest; do not guess an alias or substitute a stronger model for difficult cases. This evaluates whether the shipped authoring experience works with Luna, not whether another model can finish its output. Runtime LLM operations inside generated artifacts are a separate capability and keep their declared model policy.

### Inputs and environment

- Use short outcome-oriented requests, typos, normal follow-ups and real existing artifact context. Examples in section 9 illustrate intent, not mandatory wording or hidden templates.
- Test fresh creation, edits after a later session, populated collections, ambiguous group names and users with limited permissions. Include ordinary cases where no new backend resource is needed.
- Provide only the standard product system instructions, tool descriptions and SDK reference available to real users. Do not add scenario-specific schemas, exact tool sequences, code, hidden assertions or “remember to” hints to make a test pass.
- Clarification is legitimate when a decision is genuinely missing, particularly audience, group identity or destructive changes. Use prewritten user answers based on intent; no engineer coaching. Record clarification count and unnecessary questions.
- Use controlled test accounts/groups, files and content with no real external recipients. Exercise only current sharing/authentication and service paths against those accounts. Keep runtime content and permissions realistic.

### Prevent prompt overfitting

Keep a development set and a separate held-out set of user requests and fixture contexts. Hold out task combinations and follow-ups as well as paraphrases: for example, evaluate the same collection/file/permission primitives on a reading list or team directory, not only a blog. Expected outcomes stay in the test harness, outside the agent context.

Fix failures in general tool contracts, SDK ergonomics, errors, product instructions or implementation. Do not add a special blog recipe or expected schema to the prompt to satisfy one case. If a failure informs a change, treat that case as development evidence and confirm the change on new held-out cases. Include permission-denied and unsupported requests so “success” does not mean claiming to implement everything.

Run at least three independent authoring attempts per core journey with clean state and a frozen release candidate. Report all attempts, not the best sample. Use consistent, predeclared budgets and record timeouts/budget exhaustion as failures or incomplete outcomes. Allow normal automatic repair within the budget; label any human repair or stronger-model rescue as assisted and exclude it from unassisted success.

### Evidence and scoring

| Measure | How to assess |
| --- | --- |
| Functional completion | Use the resulting artifact in a browser and verify persistence, schemas and permissions through services. A screenshot or success message is insufficient. |
| Initial success and repaired success | Report separately, with tool calls, repair attempts, elapsed time, tokens and estimated cost. |
| Correct resource definition | Inspect fields, validation, indexes, bindings and policy. Do not require the exact naming/layout chosen by a reference implementation. |
| Preservation | Existing IDs, visualizations, records, files and access survive edits. |
| Identity and sharing | Exercise existing users/groups and supported external viewers, direct API denial and group/session revocation. |
| Honesty and usability | Correctly disclose incomplete work; usable loading/error/conflict states; no unnecessary manual provisioning. |

Set the unassisted completion target and latency/cost budgets before running the release evaluation, and report results per journey with sample counts. Three attempts are a minimum reliability check, not statistical proof. Unauthorized disclosure/writes, destructive data loss, or real side effects during fixture verification block release regardless of aggregate completion rate. Keep deterministic backend/concurrency tests as separate release gates; passing authoring evaluations cannot replace them.

## 11. Delivery and release gate

This is a large feature even with model reuse. Keep one coherent release branch, with reviewable implementation stages and disabled-by-default incomplete capabilities. Do not expand the first PR into the deferred features.

| Stage | Deliverable |
| --- | --- |
| 1. Contracts and baseline | Reconcile main, historical artifact fixtures, existing auth/sharing, deployed records, storage topology and supported limits. Agree schema-management tool/API contracts, existing group integration, the actual sharing support matrix and Luna evaluation budgets. |
| 2. Artifact foundation | Access resolver, optional publication/settings, isolated host, legacy adapters and additive migrations. |
| 3. AI authoring and records | Tool extensions, schema/resource management, records/policies/indexing, fixture validation/repair and read-only Data explorer. |
| 4. Files and execution | Documents/images, storage lifecycle, request-scoped handlers/streaming, explicit result saving and existing usage accounting. |
| 5. Sharing and inspection | Resource-local row/file permissions, unchanged sharing integration, read-only Data explorer and basic Analytics view. No analytical catalog integration in this release. |
| 6. Integrated qualification | All cases above, held-out Luna evaluations, localization/UI evidence, multi-worker failure tests, load checks, backup/restore and rollout runbook. |

Before implementation, assign owners for backend, frontend, identity/security and release. Verify existing identity/sharing behavior and resolve storage deployment, index/encryption strategy, connected-stream handling and default quotas in stage 1. These are concrete implementation decisions, not reasons to introduce another app model.

Before enablement, require unassisted Luna results under the frozen evaluation protocol; service authorization/concurrency tests; browser evidence for the user journeys; existing artifact and old/new SDK data-parity coverage; measured performance at proposed limits; two-worker restart/failure evidence; and migration/restore/rollback evidence. Follow repository procedures for UI evidence, localization and live QA during implementation.

Roll out internally, then to an opt-in cohort, then broadly after agreed numerical error/latency/capacity thresholds hold. Include per-artifact execution/upload/public-access disable controls, audit visibility and safe cleanup. Document what survives UI rollback, what is lost on stream interruption and how explicitly saved records/files remain accessible.

**Done means a user can ask for a working artifact, refine it through chat, use its records/files/streaming, share it through existing supported flows and inspect its data and views—while their existing artifacts continue to work.**


## Implementation reconciliation

Main's “Use this version” operation creates a UI version under the same artifact;
it must retain resources. It is not a copy into a distinct app with empty data.
A separate empty-data copy remains deferred. Report deletion currently archives;
resources are retained and their runtime is suspended. No new sharing semantics
or automatic detachment were introduced.

AI operations may omit `model_id`: the backend resolves the accessible report/user
model default and pins its ID in the definition. Updating without a model ID
preserves that pin. Model access is checked again for each invocation and during
streaming. The SDK reference is general; evaluation scenarios are separate.

The managed file implementation requires a private shared volume across workers.
It does not claim an object-storage adapter. Records and file bytes are encrypted;
the entire database and resource metadata are not encrypted by this feature.
The web artifact host provides the live SDK bridge. External embedded MCP hosts
and exported HTML do not provide that bridge; resource apps use their web URL.

Passing deterministic/browser checks does not satisfy the real-model authoring or
production-capacity gates above. Keep the feature disabled until those pass.
