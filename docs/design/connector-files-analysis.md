# Connector files — read & analyze (MCP)

How a file from an MCP connector (Google Drive, OneDrive, …) becomes usable in a
report: **read to answer** (agent reads content) and **read to analyze**
(download → materialize → `inspect_data`/`create_data` query it). Follow-up to
the connector catalog (`connectors-without-agents.md`); separate PR.

## Principle

**The report owns a *reference*; bytes are a *materialization* — an on-demand,
version-checked, per-user cache, never a stored attachment.** Materialization
lands the bytes in the same `File` table that uploaded files use, so the existing
analysis stack (`inspect_data` / `create_data` / `read_excel_as_csv`) consumes
them with no per-source code path.

## Auth model (not DCR)

MCP connectors use the **same OBO pattern as Fabric**: an admin registers an
OAuth client once (**system setup**, the `oauth_app` auth variant) and each user
signs in individually (**per-user auth**, `user_required`). DCR is a nice-to-have
for zero-setup servers, not a requirement. Per-user auth on MCP is **free** (not
EE) — `_user_auth_needs_enterprise("mcp") == False` (EE only for `tables`).

## Workstream A — the engine

### A1 — blob bridge (MVP, agent-driven)  ✅ this PR
Makes "analyze the Google sheet" run end-to-end for any MCP that returns a file.
- `mcp_client.aread_resource` keeps the base64 blob (was discarded after
  measuring size); `_extract_binaries` pulls file blobs from tool results
  (EmbeddedResource/blob).
- `read_mcp_resource` + `execute_mcp` materialize a binary blob via
  `attach_drive_file_to_session` → return a `session_file_id`.
- `_file_tool_common`: MIME→ext map (`ext_for_mime`) so blobs with no filename
  extension still attach; extension derived from MIME.
- Same-turn visibility: every materializer appends the new `File` to
  `runtime_ctx["excel_files"]` (the init-time snapshot isn't otherwise
  refreshed), so `inspect_data`/`create_data` later in the same turn see it.
- Flow: `list_mcp_resources → read_mcp_resource (blob→File, session_file_id) →
  inspect_data/create_data`.

### A2 — freshness: ephemeral connector files  ✅ this PR
For the **agent-driven** flow, freshness comes from re-fetching, not a cache —
so connector-materialized files are made **ephemeral per turn**:
- `File.source_kind` (`upload` | `connector`) — new column + migration.
- `attach_drive_file_to_session(..., source_kind="connector")` (the default):
  the file is stamped `connector`, made available to **this turn's** tools via
  the `excel_files` append, but **not** durably linked into `report.files`.
  Uploads (`source_kind="upload"`, via `file_service.upload_file`) stay durable.
- Result: a connector file can't be reused stale next turn (it's not in
  `report.files`); the agent re-downloads when it needs the data again — fresh,
  and fetched under the current user. Verified: connector file is in
  `excel_files` same-turn, absent from `report.files`; upload is present.

> Per-user permissions also fall out: each turn re-fetches under the requesting
> user, so one user's bytes are never persisted into a shared report.

### A3 — durable references + resolver  ✅ backend built
Pin a connector file to a report; it's re-materialized fresh, per-user, each run.
- `FileReference` model + migration — the report owns `connection_id` +
  `external_file_id` (+ name/mime); **no bytes cached on the reference**.
- `file_reference_service.ensure_materialized(db, ref, user, report, org)` —
  fetches under the current user via the connection client → ephemeral
  `connector` session File (reuses A2). Always fresh + per-user.
- `agent_v2.main_execution` resolves the report's references into
  `analysis_files` at run start (best-effort).
- Routes: `POST/GET /reports/{id}/file_references`, `DELETE /file_references/{id}`,
  `GET /connections/{id}/files` (per-user picker listing). Verified registered;
  serializer unit-tested.

**Remaining:** the **prompt-box picker UI** (browse `GET /connections/{id}/files`
→ pin) — backend endpoints are ready; building/verifying the UI needs a
connector with a working per-user token (blocked in the proxied sandbox by
Google's interactive consent + the demo MS account's MFA).

## Workstream B — connectors (replace native Drive/OneDrive/SharePoint)

Native today: `google_drive`, `onedrive`, `sharepoint` (`data_shape=files`,
EE-gated, `READ_FILE` capability). Move to MCP file connectors that materialize
via A.

- **B0 (de-risk, needs demo creds):** for each provider — register the admin
  OAuth client (Google Cloud / Entra), point at the MCP server, verify per-user
  sign-in + file read/export (Google-native → xlsx/csv). The open unknown is
  *which* MCP servers; likely self-hosted/community, not first-party.
- **B1:** add presets (`auth="oauth_app"`) + square icons.
- **B2:** coexist (native + MCP), files analyzable via A1/A2.
- **B3 (product call):** deprecate native. Note the UX shift — native gives an
  *indexed file catalog* (browse a Drive as tables); MCP gives *search/pick +
  materialize on use*. Not a like-for-like swap.

### Demo creds needed (per provider)
1. MCP server URL(s) — and whether the server owns OAuth (DCR) or needs our client.
2. OAuth client (if server uses ours): `client_id`/`secret` (+ tenant for MS),
   scopes (Drive/Gmail or Files.Read/Mail.Read + offline/openid), redirect
   `{app}/api/connections/oauth/callback`; demo account added as a test user.
3. Demo account login for per-user sign-in.
Kept only in gitignored `.env.sandbox`; rotate after. Per-user sign-in is
interactive (consent/MFA may need a manual step).

## Sequencing
```
A1 ──► A2 ──► A3
        │
B0 ──► B1 ──► B2 (needs A1/A2) ──► B3
```
A1 ships first (mock-verifiable, no creds). B0 runs in parallel once creds land.
