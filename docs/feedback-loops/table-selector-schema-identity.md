# Feedback Loop — identify the schema shown in the table selector

The selector previously displayed a generic Reload button without identifying whose schema it showed. The first identity implementation covered only Power BI and added a second refresh button per Power BI connection. On a mixed agent, the generic Reload still refreshed every connection, so its scope and outcome were unclear.

## Reproduction and verification

Capture the real selector and Connections modal with synthetic connection/table API responses. The current design keeps identity selection in the modal and separates table-list reload from connection schema discovery.

Start the frontend locally on port 3100:

```bash
cd frontend
npm run dev -- --port 3100
```

In another terminal, from the repository root:

```bash
node tools/agent/verify_schema_identity.cjs
```

The script temporarily creates a public preview page, mounts the real `TablesSelector` and `AgentConnectionsModal`, intercepts API calls with synthetic responses and removes the page on exit. It never reads or changes a live Power BI environment. Do not run the preview page on a public server.

Observed: **8 browser scenarios passed** — a non-Power-BI delegated table connection, a mixed Power BI/SQL agent, a shared-only connection, partial model failure, failed job, disconnected account, reader without account-switch permission, and Hebrew RTL. Assertions verify that Reload rereads the table list without starting a schema job; its chevron opens scoped choices even for a sole action; the modal switches query identity and updates available actions; a reader cannot switch identity; the toolbar spinner and result notification appear during and after a schema job. No browser exceptions occurred.

Evidence: `media/pr/powerbi-schema-identity/after-refresh-menu-mixed.png` shows the previous identity strip and menu. `after-reload-menu-mixed.png`, `after-connections-toggle.png`, `after-reload-loading.png`, `after-reload-menu-he.png`, and `reload-menu-flow.gif` show the latest design. Partial and failed screenshots are in the same directory.

## Implementation

- `frontend/components/AgentConnectionsModal.vue` shows a small account toggle for a delegated connection when the user may manage it. Switching refreshes the parent view. Its connection-test control is labeled “Test connection” so it cannot be mistaken for schema refresh.
- `frontend/components/datasources/SchemaIdentityStatus.vue` remains mounted as a headless refresh controller. It starts a personal or shared background job, polls the matching scope, and reports success, partial results or failure through a toast. Completion reloads the displayed tables.
- `backend/app/routes/data_source.py` includes registry data shape and catalog ownership in the existing agent-connections response.
- `frontend/components/datasources/TablesSelector.vue` has a split Reload control. The main button rereads the existing catalog without invoking discovery. The chevron opens connection-specific schema refresh actions with icons and scope labels; this is consistent when one or many actions are available. `Spinner.vue` appears in the chevron while a schema job runs.
- New strings are included in all ten locale catalogs with matching key shapes.

## Limits

The browser loop verifies the actual UI with mocked API boundaries, not a deployed full-stack refresh. The supplied provider credentials were not used in the synthetic loop. Existing backend regression failures after merging main remain unresolved by this UI change. The broader locale sweep was attempted but stopped after the smoke/sign-in pages failed to render their expected content in this local setup; no full-suite success is claimed. The focused Hebrew menu scenario passed and its screenshot was inspected.
