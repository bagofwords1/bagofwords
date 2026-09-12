# Feedback loop — multiple connections requiring sign-in

An agent with five missing personal sign-ins previously authorized the first connection immediately, without offering a choice. Any missing sign-in hid all content sections, even when another connection was usable.

## Reproduction

With the frontend dev server on port 3100:

```sh
cd frontend
node tests/instructions/knowledge-multi-signin.mjs
SINGLE=1 node tests/instructions/knowledge-multi-signin.mjs
node tests/data_sources/connection-signin-status.mjs
```

The multi-connection script mounts the real KnowledgeExplorer on a temporary unauthenticated preview route, stubs only API boundaries, and removes the route when finished. No live credentials or external sign-ins are used. Before the fix, `BEFORE=1` recorded an immediate authorization request for the first of five connections. After the fix, no authorization starts until the user chooses a row.

## Fix

- `KnowledgeExplorer.vue`: one or multiple pending connections open AgentConnectionsModal (updated at user request). The tree badge stays “Sign in,” without a count. Content sections remain available when at least one connection does not require sign-in. The agent's connection chips use shared status colors.
- `useConnectionStatus.ts`: shared action predicate excludes service-account fallback.
- `AgentConnectionsModal.vue`: agent icon, name and description above a divided list; per-row status, available resource counts, and sign-in action. OAuth targets the clicked connection, disables repeat actions and retains its spinner through navigation. Manual fallback receives the selected connection's type/id and the actual agent id; saving refreshes the list. Existing connection management permissions/actions remain.
- Counts use embedded viewer-scoped fields. File-shaped catalogs use the file label. Unknown/zero counts and counts before sign-in are omitted rather than replaced with shared discovery totals. Existing translated labels are reused.

## Verification

Browser checks cover five pending actions, choosing the third connection, row loading state, one signed-in connection with four remaining actions, accessible tree sections, resource counts, and Hebrew. The independent single-connection regression covers direct authorization, duplicate-click suppression, error cleanup and redirect loading state. Shared status regression keeps service-account and actual-failure behavior intact.

Evidence lives in `media/pr/knowledge-multi-signin/`: before, after, loading, partial-access, Hebrew and mobile screenshots, plus flow recording. Capture after transition completion; preview routes must run sequentially because Nuxt route regeneration can reload other previews.

This verifies UI routing and API boundaries, not a real provider login. OAuth uses full-page navigation; the user returns to the agent and can reopen Sign in for remaining connections.

Single-connection follow-up: the tree no longer starts OAuth directly. `SINGLE=1` verifies one row, no authorization request on opening, and targeted authorization on the row button. Evidence: `single.png`. The older direct tree-spinner test describes superseded behavior.
