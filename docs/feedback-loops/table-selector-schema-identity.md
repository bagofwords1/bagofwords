# Feedback Loop — identify the schema shown in the table selector

The Power BI table selector previously displayed a generic Reload button without identifying whose schema it showed. A successful shared refresh could therefore be mistaken for a successful personal refresh.

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

Observed: **7 browser scenarios passed** — personal refresh, shared refresh, partial model failure, failed job, disconnected account, reader without account-switch permission, and Hebrew RTL. The personal scenario also opens the existing account dialog and switches identity. Assertions verify personal/shared endpoint selection, matching job scope, catalog reload after completion, permission gating, and no browser exceptions.

Evidence: `media/pr/powerbi-schema-identity/` contains before/after screenshots, Hebrew and warning/error states, and a recorded refresh flow.

## Implementation

- `frontend/components/datasources/SchemaIdentityStatus.vue` shows Power BI's effective identity and opens the existing `ConnectionDetailModal` when switching is permitted.
- Refresh starts the existing personal or shared background job and polls the matching scope. Completion reloads the displayed tables. Partial/failed results remain visibly distinct from success.
- `frontend/components/datasources/TablesSelector.vue` includes one strip per Power BI connection in its connection filter. Non-Power-BI selectors retain their existing refresh control.
- Unknown timestamps are shown as unknown; credential-use timestamps are never presented as schema refresh times. Partial results label their time as the last refresh attempt.
- New strings are included in all ten locale catalogs. Key drift is unchanged relative to HEAD.

## Limits

The browser loop verifies the actual UI with mocked API boundaries, not a deployed full-stack refresh. Existing backend regression failures after merging main remain unresolved by this UI change. The broader locale sweep was attempted but stopped after the smoke/sign-in pages failed to render their expected content in this local setup; no full-suite success is claimed. The focused Hebrew scenario passed and its screenshot was inspected.
