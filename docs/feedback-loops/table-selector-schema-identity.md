# Feedback Loop — identify the schema shown in the table selector

The selector previously displayed a generic Reload button without identifying whose schema it showed. The first identity implementation covered only Power BI and added a second refresh button per Power BI connection. On a mixed agent, the generic Reload still refreshed every connection, so its scope and outcome were unclear.

## Reproduction and verification

Before changing the selector, capture the real component with synthetic connection/table API responses. The before screenshot has no identity or refresh timestamp. After the change, the same component shows the effective account, the matching indexing timestamp and the refresh outcome.

Start the frontend locally on port 3100:

```bash
cd frontend
npm run dev -- --port 3100
```

In another terminal, from the repository root:

```bash
node tools/agent/verify_schema_identity.cjs
```

The script temporarily creates a public preview page, mounts the real `TablesSelector`, intercepts API calls with synthetic responses and removes the page on exit. It never reads or changes a live Power BI environment. Do not run the preview page on a public server.

Observed: **8 browser scenarios passed** — a non-Power-BI delegated table connection, a mixed Power BI/SQL agent, a shared-only connection, partial model failure, failed job, disconnected account, reader without account-switch permission, and Hebrew RTL. The personal scenario also opens the existing account dialog and switches identity. Assertions verify that a direct click runs the sole action, a multi-action click opens a menu without starting a job, menu choices select the named connection and scope, and completion reloads the catalog. No browser exceptions occurred.

Evidence: `media/pr/powerbi-schema-identity/after.png` shows the first implementation with a per-connection button. `after-refresh-menu-single.png`, `after-refresh-menu-mixed.png`, `after-refresh-direct.png`, `after-refresh-menu-he.png`, and `refresh-menu-flow.gif` show the revised control. Partial and failed screenshots are in the same directory.

## Implementation

- `frontend/components/datasources/SchemaIdentityStatus.vue` shows each table connection's effective identity and opens the existing `ConnectionDetailModal` when switching is permitted.
- Refresh starts the existing personal or shared background job and polls the matching scope. Completion reloads the displayed tables. Partial/failed results remain visibly distinct from success.
- `backend/app/routes/data_source.py` includes registry data shape and catalog ownership in the existing agent-connections response.
- `frontend/components/datasources/TablesSelector.vue` keeps the connection names in one compact summary. Its toolbar has one refresh control: direct when exactly one action is available, a full-button menu with named connection/scope choices otherwise. The old agent-wide Reload is used only when no table connection can be identified.
- Unknown timestamps are shown as unknown; credential-use timestamps are never presented as schema refresh times. Partial results label their time as the last refresh attempt.
- New strings are included in all ten locale catalogs. Key drift is unchanged relative to HEAD.

## Limits

The browser loop verifies the actual UI with mocked API boundaries, not a deployed full-stack refresh. The supplied provider credentials were not used in the synthetic loop. Existing backend regression failures after merging main remain unresolved by this UI change. The broader locale sweep was attempted but stopped after the smoke/sign-in pages failed to render their expected content in this local setup; no full-suite success is claimed. The focused Hebrew menu scenario passed and its screenshot was inspected.
