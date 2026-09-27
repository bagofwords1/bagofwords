# Artifact App Persistence — records that page artifacts can read and write

Status: implemented as a POC on `feature/artifact-app-persistence` (page artifacts only).
Scope: storage declaration, `app_records` table, four data endpoints, runtime `useCollection`,
host bridge, agent tool gates and approval of destructive declaration changes.
Out of scope (deliberately): generated server logic, external actions, anonymous writes,
live push, paging/search, apps outside reports, iframe origin isolation.

## Summary

A page artifact may declare `content.storage`: named collections with sharing rules and typed
fields. Generated code calls `useCollection("notes")` and gets `items / loading / error / add /
update / remove / refresh`. The runtime never talks to the API: it posts `APP_DATA_REQUEST` to
the host page, the host calls `/api/artifacts/{artifact_id}/data/...` with its own session, and
the server enforces every rule. Artifacts without `storage` behave exactly as before and never
query `app_records` (so an unmigrated database keeps working for them).

## Problem and chosen shape

Generated dashboards could show data but not remember anything a viewer did (notes, checklists,
votes). Three shapes were considered:

| Option | Why not / why |
|---|---|
| Generated backend code per app | Rejected: runs model-written code on the server, needs a sandbox and review, and every app becomes its own security surface. |
| Declared-schema tables (one real table per collection) | Rejected: DDL at agent time, per-dialect migrations, and a schema change becomes a data migration. |
| **Generic record store + declared schema (chosen)** | One fixed table; the declaration is data validated by the server; schema changes are read-time, so rows are never rewritten. |

## Data model

One table `app_records` (migration `apprec01`, portable `sa.JSON`, no JSONB): a fixed envelope the
server filters on — `organization_id`, `report_id`, `artifact_id` (FK `artifacts.id`),
`collection`, `user_id` (author), `version`, BaseSchema timestamps incl. `deleted_at` — plus
`data` (EncryptedJSON) holding the app's fields. Rows hang off the stable `Artifact.id`, not a
version, so they survive edits, rebuilds with `replaces_artifact_id`, and owner reruns. The
server never filters inside `data`, so behavior is identical on SQLite and Postgres.

Limits (`schemas/app_storage.py`): 20 collections, 50 fields, 64 KB of non-json fields per record,
256 KB per json field and per record, 10,000 live records per collection.

## Declaration and access rules

The declaration lives in `ArtifactVersion.content.storage`. The effective declaration is the one
on the latest completed version; an unparsable stored declaration counts as no storage (fail
closed). Field types: `string` (optional `max_length`), `number`, `boolean`, `date`, `json`;
fields may be `required` and have a `default`. Unknown keys are rejected.

Two layers, and the second can only narrow the first:

1. **Layer 1** is the report's existing artifact visibility (`report_service._check_visibility`):
   none / shared / internal / public. No admin bypass: an org admin who is not the owner has no
   extra app-data rights.
2. **Layer 2** is the collection rule. `scope: shared` needs `create: members|owner` and
   `modify: author|owner`; `scope: per_user` means each user sees and changes only their own rows.

Principals: owner, member (org member or share recipient), outsider (signed in but neither),
anonymous. Anonymous and outsider access is derived, not declared: they may read a collection
only when it is `shared` with `create=owner` (owner-published content), and they never write.

## Save round trip over the host bridge

1. Generated code calls `notes.add({...})`; the runtime (`public/libs/artifact-globals.js`) posts
   `APP_DATA_REQUEST {rid, op, collection, id?, data?, version?}` to `window.parent`.
2. The host (`ArtifactFrame.vue` main or fullscreen iframe, `pages/r/[id]`) accepts it only from
   its own iframe window (`event.source`), then `utils/artifactAppData.ts` builds the URL. Path
   segments are allowlisted (collection name pattern, record id pattern), not just encoded,
   because `..` would otherwise steer the host's session at other `/api/artifacts/...` routes.
3. The host replies `APP_DATA_RESULT` with the same `rid` to the requesting window only.

The host is the credential boundary: the iframe never receives a token. Errors come back as
codes (`conflict`, `forbidden`, `unauthenticated`, `validation`, `too_large`, `limit_reached`,
`not_found`, `collection_not_declared`, plus host-side `unavailable`, `network`, `timeout`).
Rejections are tagged so an uncaught app-data failure never becomes `ARTIFACT_ERROR`.

## Server checks

Routes: `GET/POST /api/artifacts/{artifact_id}/data/{collection}`,
`PATCH/DELETE .../{collection}/{record_id}` with optional auth. Every op runs, in order:
identity (writes need a user) → artifact and report exist in one org → Layer 1 → collection
declared → Layer 2 rule (per_user rows of others are 404, not 403) → record validation and limits
→ version check (update/delete) → write + audit (`app_data.record_*`, never record content) in
one transaction. `user` and `mine` are computed server-side; the stored author is always the
session user.

## Concurrency

Optimistic: PATCH and DELETE send the version they hold; the server runs one conditional
`UPDATE ... WHERE id AND version AND deleted_at IS NULL`. Zero rows → re-read → 404 if gone,
else 409 `app_data.conflict` with `current_version`. The runtime sets `error.code = "conflict"`
and refreshes the list. Deletes are soft.

## App changes

Rows are never rewritten. Reads apply the current declaration: missing fields get their
declared default, removed fields are hidden (and come back on revert), changed types are
returned as stored. A new required field in an existing collection must have a default.

Destructive changes need the user's approval: `collection_removed`, `field_removed`,
`field_type_changed`, `scope_changed`, `create_changed`, `field_made_required` (without default)
and `collection_readded` (live orphaned rows would become visible again). They are detected
against the effective declaration, even for empty collections (impact 0 is shown).
`create_artifact` / `edit_artifact` pause with a durable builtin confirmation
(`stream_user_confirmation`; only the run's user can answer; card `StorageChangeApproval.vue`).
Deny, timeout, a non-interactive run or too little tool budget left fail closed: nothing is
persisted. Paths that cannot ask — MCP edit and legacy edit — refuse any edit that would change
the effective declaration. The legacy `/api/artifacts/confirm/{id}` route refuses builtin ids.
Omitting `storage` carries the declaration forward; `{"collections": {}}` removes it.

## Hosts

| Host | Behavior |
|---|---|
| In-app `ArtifactFrame` (main + fullscreen) and public `/r/{id}` | Real API via host session |
| Headless validation, thumbnails, PDF | In-memory store, empty collections |
| HTML export (`artifact-offline-host.js`), MCP app | `unavailable` error, no hang |
| Browser verification preview broker | Reads of this artifact proxied; writes 403 and recorded `blocked` |
| Anything unknown | Runtime `timeout` after 20 s |

## Security notes

- **Pre-existing risk, not introduced here:** artifact iframes are `srcdoc` with
  `sandbox="allow-scripts allow-same-origin ..."`, so generated code runs in the app origin and
  can read the viewer's auth token. Verified live: `document.cookie` in the `about:srcdoc` context
  contains `auth.token`. The cookie settings in `nuxt.config.ts` are not applied by
  `@sidebase/nuxt-auth` 0.9.3, which uses its `token.cookieName` default `auth.token`.
- This design keeps the iframe credential-free (all calls go through the host), so
  `allow-same-origin` can later be removed without redesigning app data.
- The server trusts nothing from the iframe: identity, author, `mine` and rules are server-side.

## Known limits and accepted gaps

- The 10,000-record cap is soft under concurrent creates (count then insert, no lock).
- No list pagination; a list returns every visible live record.
- Owner-only `PATCH` of artifact content (replace) and duplicating an older version can change
  the declaration without the confirmation step (the declaration is still validated).
- The runtime cannot tell the viewer whether they are the report owner.
- The `useCollection` reference gate uses a small lexer that does not model regex literals; the
  server still enforces the declaration, so a missed reference only fails at runtime.
- 30 pre-existing flat dotted `errors.*` keys (e.g. `"artifact.not_found"`) never resolve in
  vue-i18n; the new `errors.app_data.*` keys are nested and do resolve.

## Tests that prove the key properties

- Access matrix per visibility, principal and rule (non-widening, anonymous never writes):
  `backend/tests/e2e/rbac/test_app_data_access_matrix.py`, pure table in
  `backend/tests/unit/test_app_data_rules.py`.
- Read-time defaults, limits, optimistic concurrency, effective declaration, survival across
  versions and reruns, audit: `backend/tests/e2e/rbac/test_app_data_records.py`
  (also run with `--db=postgres`, with `backend/tests/unit/test_app_record_schema.py`).
- Tool gates and carry-forward on every version-minting path:
  `backend/tests/e2e/test_artifact_storage_tools.py`, `backend/tests/unit/test_artifact_storage_gates.py`.
- Approval, deny, timeout, fail closed, MCP/legacy refusal:
  `backend/tests/e2e/test_artifact_storage_confirmation.py`.
- Runtime and non-app hosts (Playwright): `backend/tests/e2e/test_app_data_runtime.py`.
- Host bridge and approval card: `frontend/tests/unit/artifactAppData.mjs`,
  `artifactAppDataHosts.mjs`, `artifactErrorBoundary.mjs`, `storageApprovalPlacement.mjs`.
