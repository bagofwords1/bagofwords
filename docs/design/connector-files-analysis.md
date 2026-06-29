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

### A2 — reference model + resolver (freshness + permissions)
- `FileReference` (durable, report-associated): `connection_id`,
  `external_file_id`, `name`, `mime`, `etag`/`modified_at`, `last_fetched_at`;
  `source_kind` (upload|connector) on `File`.
- `ensure_materialized(ref, user)`: etag-checked, per-user fetch via the A1
  bridge → `File` cache; reuse when unchanged.
- `excel_files` builder = uploads (report-wide, durable) **+**
  `ensure_materialized(ref)` for in-scope connector references (mirror the
  existing per-completion image-file filter). `inspect_data`/`create_data`
  unchanged. → no stale reuse; one user's bytes never served to another.

### A3 — prompt-box file picker (optional)
Pick a file when composing (`list_mcp_resources`/search) → create a
`FileReference` → rides A2. Read-to-answer uses the same resolver.

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
