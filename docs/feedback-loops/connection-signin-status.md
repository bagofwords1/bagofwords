# Feedback loop — missing personal sign-in appears red

The Connections list classified Power BI set to Me, without a personal token, as an error. This change distinguishes missing access from a failed connection across consumers of the shared status helper.

## Root cause

`frontend/composables/useConnectionStatus.ts:getEffectiveStatus` grouped `offline` and `not_connected` as errors. `backend/app/services/connection_identity.py:build_token_identity_status` deliberately returns `effective_auth=none`, `has_user_credentials=false`, and `connection=offline` for Me without a token. No backend change is needed.

## Reproduce and verify

From the repository root:

```sh
node frontend/tests/data_sources/connection-signin-status.mjs
```

Before: assertion failed, actual `error`, expected `sign_in_required`.
After: PASS. Covers missing access across connector types; shared indexing must not imply personal access; explicit errors, stored-credential failures and service-account failures retain error status; normal indexing and unknown states remain intact.

Browser evidence, with the frontend dev server on port 3100:

```sh
cd frontend
node tests/data_sources/connection-signin-evidence.mjs
```

Uses the real AgentConnectionsModal, a temporary preview route and synthetic API boundaries. Removes the route on completion. `BEFORE=1` captures the baseline on pre-fix code. No external sign-in or customer credentials are used.

Before/after: `media/pr/connection-signin-status/before.png`, `after.png`, and Hebrew `he.png`. Browser assertions passed without page errors. All ten catalogs contain the new localized label; no existing keys were changed or removed.

## Scope

The shared helper covers KnowledgeExplorer connection dots, AgentConnectionsModal badges, legacy data layout/table dots and connection badges. ConnectionDetailModal now uses the same dot colors and status label rather than its separate amber fallback. KnowledgeExplorer's organization list dots include a localized status tooltip. Actual failed tests still override badges where the UI displays a fresh explicit test result.

This is a presentation fix, not an authentication or token-validity change. Verification uses backend-shaped synthetic payloads, not a live Power BI login.
