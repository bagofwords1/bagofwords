# Feedback Loop — delegated-OAuth connections 401 mid-use with no way to reconnect

A per-user OAuth connection (Gmail, Power BI, Fabric, SharePoint, OneDrive,
Outlook) fails mid-use with a raw provider 401 — e.g.
`search_email failed: Gmail API 401: Request had invalid authentication
credentials` — once its short-lived access token expires. The connection still
reports itself as connected (`has_user_credentials=true`), so the UI offers no
reconnect affordance, and the user has to guess that they must disconnect and
reconnect. Entra **OBO** connectors are the worst case: the OBO grant returns no
refresh token at all, so those tokens can never self-heal.

Validated in a fresh sandbox — Loop A deterministically (mock OAuth) and Loop B
live against a real Entra tenant on 2026-07-19.

---

## Root cause (validated)

1. **Silent stale-token hand-off.** `ConnectionService.resolve_credentials`
   (`backend/app/services/connection_service.py`) called
   `maybe_refresh_oauth_credentials`, which — when there was no refresh token or
   the refresh failed — logged a warning and returned the **expired** creds
   unchanged (`connection_oauth_service.py`). The dead token was then handed to
   the provider, producing an opaque 401 downstream instead of a typed
   "reconnect" signal.

2. **Status ignored expiry.** `build_token_identity_status`
   (`backend/app/services/connection_identity.py`) and
   `build_user_status_for_connection`
   (`backend/app/services/user_data_source_credentials_service.py`) set
   `has_user_credentials=True` whenever a credential **row exists**, never
   comparing `expires_at` to now. An expired, un-refreshable token therefore
   reported as healthy, so no Connect/Reconnect button was shown.

3. **Tool surface swallowed it.** `resolve_file_client`
   (`backend/app/ai/tools/implementations/_file_tool_common.py`) turned the
   failure into a flat `"Failed to construct client: ..."` string with no
   machine-readable reconnect intent.

4. **OBO can't refresh, and re-login clobbered a good reconnect.** The Entra OBO
   grant (`exchange_obo_token`) returns no refresh token, so OBO rows expire
   un-renewable. Worse, `auto_provision_connection_credentials` only skipped
   re-provisioning when the existing token was *unexpired* — so after a user did
   an interactive reconnect (which DOES yield a refresh token), the next login
   would re-OBO over that durable row and drop the refresh token again.

The Google half of this class of bug (no refresh token after re-consent) was
fixed earlier by sending `prompt=consent`; this loop covers the general
expiry/reconnect surface for every delegated connector, plus the OBO durability
gap.

---

## The fix

- **Typed reconnect signal.** `maybe_refresh_oauth_credentials` now returns
  `(creds, reconnect_required)`; `resolve_credentials` raises a 403 whose detail
  is a dict `{"code": "reconnect_required", "connection_id", "connection_type"}`
  (and `"connect_required"` for never-connected) instead of returning a dead
  token. `oauth_creds_need_reconnect(creds)` is the network-free predicate
  (expired ∧ no refresh token).
- **Status reflects validity.** `DataSourceUserStatus` gains `needs_reconnect`;
  both status builders set it via `_row_needs_reconnect(row)`. `has_user_credentials`
  is unchanged (a row still exists) — the UI now distinguishes healthy from
  expired.
- **Actionable tool error.** `resolve_file_client` forwards the typed detail as
  an actionable message tagged `[reconnect_required]` instead of the opaque
  string.
- **OBO hybrid (durable upgrade).** `auto_provision_connection_credentials`
  skips any row that already holds a usable refresh token
  (`refreshable_credentials_exist`), so an interactive reconnect permanently
  upgrades an OBO row and a later login never clobbers it. Presence/absence of a
  refresh token distinguishes an interactive credential from an OBO one — no
  schema change needed.
- **Frontend.** `ConnectionDetailModal.vue` shows an amber "session expired —
  Reconnect" banner + button for every identity branch;
  `useDataSourceConnect.ts` and `DataSourceSelector.vue` treat `needs_reconnect`
  the same as "not connected" so the agents surfaces prompt reconnect too.

---

## Loop A — deterministic reproduction (mock OAuth, no external services)

```bash
cd backend && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db"; mkdir -p db
uv run --extra dev python -m pytest tests/e2e/test_reconnect_on_expiry.py --db=sqlite -q
```

Pre-fix (assertions fail): `DataSourceUserStatus` has no `needs_reconnect`
attribute and `resolve_credentials` returns stale creds instead of raising.

Post-fix: `5 passed` — expired-no-refresh row → `needs_reconnect=True` +
`reconnect_required` 403; valid token unaffected; OBO row flagged; a login does
not clobber a refreshable (reconnected) row.

Full OAuth regression: `tests/e2e/test_connection_oauth_flow.py`,
`tests/e2e/test_obo_admin_catalog_before_signin.py`,
`tests/unit/test_connection_oauth.py` → 67 passed. Pre-existing unrelated
failures (reproduce with this change stashed): `test_ms_fabric`,
`test_obo_exchange_ms_fabric`, and the two `TestUserOverlaySync` tests assert the
old `api.fabric.microsoft.com` scope.

## Loop B — live confirmation (real Entra tenant, secrets via env only)

```bash
export BOW_ENTRA_TENANT_ID=... BOW_ENTRA_CLIENT_ID=... BOW_ENTRA_CLIENT_SECRET=...
export BOW_OAUTH_TEST_DEMO1_EMAIL=... BOW_OAUTH_TEST_DEMO1_PASSWORD=...
cd backend && uv run python ../tools/agent/live_obo_reconnect_verify.py
```

The ROPC grant stands in for the browser OAuth (the sandbox egress proxy blocks
Chromium's TLS to the Microsoft login page). Observed 2026-07-19 — 10/10 checks:

```
A. OBO row (expired, no refresh token):
   has_user_credentials True; needs_reconnect True; resolve_credentials -> 403 reconnect_required
B. Interactive reconnect (real delegated token):
   delegated token carries a refresh token; needs_reconnect False; resolve returns the real token
C. Durability (expired access token, refresh token retained):
   auto_provision skips as refreshable_credentials_exist (no OBO clobber);
   refresh token preserved; resolve performs a REAL Entra refresh (new access token issued);
   needs_reconnect stays False
```

---

## What this proves

- An expired delegated token is now a **typed, surfaced** condition, not a
  silent 401: status reports `needs_reconnect`, query resolution raises
  `reconnect_required`, and the UI offers one-click Reconnect.
- The OBO durability gap is closed against a **real** Entra tenant: an
  interactive reconnect upgrades an OBO row to a refreshable one, a later login
  won't undo it, and the refreshable row self-heals via a real token refresh.
- A still-valid token is untouched (no over-triggering).
