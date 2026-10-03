# ConnectionDetail: distinguish personal and shared schema refresh

Query access groups query identity and personal refresh/disconnect. Shared
schema groups the service-account discovery result, refresh action and schedule.
The existing permission gates remain in place. All new labels cover ten locales.

The shared block previously fetched `/indexing` with automatic scope: a newer
personal run could appear above the admin's shared Reindex button. It now requests
`scope=org` for catalog connectors and excludes user-scoped initial snapshots.
Tool-only connectors retain their existing scope and labels.

Run from frontend with the local dev server on 3100:

```sh
node tests/data_sources/connection-detail-sections.mjs
```

Passed: an initial personal result of 58 objects is not displayed as the shared
30-object result; personal and shared actions call their respective endpoints;
selecting Service account hides personal refresh but retains shared refresh;
non-admin viewers have query access but no shared section; Hebrew RTL; no page
errors. Tests use the real component with synthetic API responses and permissions.

Evidence is in `media/pr/connection-detail-sections/`, including recorded video.
Before reference is the user's supplied screenshot; after uses synthetic data.
No connector emissions or refresh-job implementation changes were made.
