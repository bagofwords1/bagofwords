# Feedback loop — directory profile disappears when SSO and Fabric use different Entra apps

Reproduced on main `c650792ae` and fixed on `codex/entra-directory-token-isolation`.
The customer uses SSO app A to sign in and requests a custom scope exposed by Fabric app B.
The resulting access token works as B's OBO assertion but cannot call Microsoft Graph.
Directory sync must use A's refresh grant to obtain a separate Graph token without replacing B's assertion.

## Root cause (validated)

In `backend/app/ee/oidc/profile_service.py`, `fetch_profile_fields` supplied the fresh login access token directly to Graph. On HTTP 401 it attempted OBO with the SSO app's credentials. Entra rejects that exchange when the assertion targets the other app. This path bypassed the existing refresh helper even when a refresh token was available.

The expired-token and successful OBO paths also wrote Graph access tokens into `OAuthAccount.access_token`, replacing the assertion used by Fabric recovery. Both defects are covered by regression tests.

## Loop A — deterministic reproduction

From `backend`, Python 3.12 with dev dependencies installed:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db \
  .venv/bin/python -m pytest tests/unit/test_entra_profile_token_isolation.py -q
```

The first four cases were run before the fix: **4 failed**. Fresh-login cases raised `EntraReauthRequired`; expired-login cases failed `assert 'graph-access' == 'fabric-assertion'`.

The fix makes those same four cases pass. Expanded checks cover admin/member memberships, fresh/expired login, repeated reads, a direct Graph login, legacy same-client OBO without refresh tokens, missing/revoked refresh tokens, empty provider responses, network failure, preservation of old profile attributes, redacted logging, and concurrent login token rotation.

Focused regression command:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db .venv/bin/python -m pytest \
  tests/unit/test_entra_profile_token_isolation.py \
  tests/unit/test_entra_profile_obo.py \
  tests/unit/test_graph_profile_fallback.py \
  tests/unit/test_obo_recovery_per_user.py -q
```

Observed: **25 passed on SQLite** and **25 passed on PostgreSQL**. For PostgreSQL use a disposable test database with `TEST_DATABASE_URL` and `--db=external`; the test fixture recreates its schema.

## Loop B — live confirmation

Used a demo user with **different** SSO and Fabric / Power BI app registrations.

### Verified permission separation (8 October 2026)

Read both app registrations and all service-principal delegated consent grants and application-role assignments through an existing demo administrator Azure CLI session. No permissions were changed.

- **SSO app**: configured and consented delegated Graph `User.Read`, plus `access_as_user` on the Fabric app. **No direct Power BI, Azure SQL, or Storage delegated grants; no application-role grants at all.**
- **Fabric/PBI app**: consented Power BI `Workspace.Read.All Dataset.Read.All`, Azure SQL `user_impersonation`, and Azure Storage `user_impersonation` (plus unrelated demo integration permissions).

Thus the live test used the requested permission separation: the SSO client did not have direct Fabric/PBI grants. See the [sanitized consent/role summary](entra-directory-token-isolation/permissions-audit.json). The remaining deployment difference is intentional: the fixed two-client directory flow was tested with `offline_access`, which the customer's old configuration lacked.

A real interactive authorization-code + PKCE login requested only `openid profile email offline_access` and Fabric's exposed custom API scope. No Graph resource scopes were mixed into this login request. Tokens were kept private. No customer production configuration or database was changed.

Before editing production code, the actual profile service failed with `EntraReauthRequired` while the actual Power BI OBO exchange and `executeQueries` returned HTTP 200 and value 1. See [sanitized before results](entra-directory-token-isolation/live-before.json).

After the fix, the same live token pair successfully:

1. Retrieved `jobTitle` and `companyName` through the SSO client and persisted them on the local membership.
2. Preserved the original Fabric assertion and its expiry.
3. Queried Power BI successfully: HTTP 200, value 1.
4. Read the directory profile again without corrupting that assertion.
5. Recovered a missing Power BI credential through `recover_stored_login` after the test marked the assertion's expiry metadata past due. This exercised the SSO refresh token after directory sync rotated it.
6. Queried Power BI again: HTTP 200, value 1, then successfully read directory attributes again.

See [sanitized after results](entra-directory-token-isolation/live-after.json). These are measured service results, not a verification of the customer's deployment.

### Rerun live verification

The reusable script is `backend/tests/integrations/verify_entra_directory_powerbi.py`.
It creates and removes its own temporary SQLite database and invokes real BOW profile, OBO, and stored-login recovery services against Microsoft. It does not change Entra settings.

Supply these environment variables through a private local environment or secret manager; do not put their values in this document or shell history:

- `ENTRA_TEST_TENANT_ID`
- `ENTRA_TEST_SSO_CLIENT_ID`, `ENTRA_TEST_SSO_CLIENT_SECRET`
- `ENTRA_TEST_FABRIC_CLIENT_ID`, `ENTRA_TEST_FABRIC_CLIENT_SECRET`
- `ENTRA_TEST_API_SCOPE`: the complete custom scope exposed by the Fabric app
- `ENTRA_TEST_ACCESS_TOKEN`, `ENTRA_TEST_REFRESH_TOKEN`: a fresh SSO login's token pair for that custom scope, with `offline_access`
- `ENTRA_TEST_PBI_GROUP_ID`, `ENTRA_TEST_PBI_DATASET_ID`: a demo dataset the signed-in user may query

The demo user must have at least one populated directory field from the script's selection. From `backend`:

```sh
.venv/bin/python tests/integrations/verify_entra_directory_powerbi.py
```

Expect `directory_sync: PASS`, all preservation/recovery checks true, and both Power BI calls `{http: 200, value: 1}`. The script exits unsuccessfully when any of those invariants fails.

## Fix and deployment requirements

- On Graph 401, try the SSO refresh grant before the legacy same-client OBO fallback.
- Keep Graph access tokens request-local. Never replace the saved login assertion or its expiry.
- Persist only a valid rotated SSO refresh token, conditionally on the account still holding the token pair used by the request. A concurrent newer login wins.
- Keep failed syncs from wiping existing directory attributes. Log HTTP status and numeric AADSTS codes, never raw provider bodies or exception messages.
- No database migration or customer OAuth application protocol changes.

The customer must deploy the backend fix, retain delegated Graph `User.Read` consent on the **SSO** app, and request `offline_access` alongside the Fabric custom scope in `bow-config.yaml`. Users whose old login has no refresh token need a fresh Entra authorization. A backend release cannot manufacture that missing token.

Example (preserve their real provider name, issuer, client credentials and custom scope):

```yaml
scopes:
  - openid
  - profile
  - email
  - offline_access
  - api://<fabric-client-id>/<customer-scope-name>
```

`User.Read` remains consented in Entra; the backend requests it separately when acquiring the Graph token. Do not add it back to this resource-specific login scope list. Organization directory-profile sync must remain enabled with the desired fields.

This verifies live service behavior and database persistence, not a full AI-generated report, a customer deployment, group synchronization, or the custom OAuth application's entire browser flow. Group overage/name resolution has its own Graph permission/token path and is not changed here.

References: [Microsoft authorization-code flow](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-auth-code-flow), [refresh tokens](https://learn.microsoft.com/en-us/entra/identity-platform/refresh-tokens), [OBO audience requirements](https://learn.microsoft.com/en-us/entra/identity-platform/v2-oauth2-on-behalf-of-flow).
