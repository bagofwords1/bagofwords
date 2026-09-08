# Documentum Connector — Delivery Plan

**Status:** Plan only. Consolidates the four research documents:
[analysis](documentum-connector-analysis.md) · [lab access](documentum-lab-access.md) ·
[auth design](documentum-auth.md) · (this file). Dates are relative to a go decision.

## Decision summary
- Build a **native `documentum` connector, `data_shape="files"`**, over Documentum REST Services (`/dctm-rest`), modeled on `sharepoint_onprem` + `network_dir`. No MCP preset, no third-party library, plain `requests`.
- Auth variants, in build order: `userpass` → `service` (OTDS client credentials / Basic service account) → **`otds_impersonation` overlay** (primary per-user path) → `oauth` (OTDS authorization code).
- Validate against a **mock first**, then a **customer/partner non-production repository**, then our **own lab** once partner entitlement lands.
- Gate: `dev_only=True` and a "verification boundary" note until §7 of the auth doc is closed on a real 23.4+/OTDS instance.

## Phase 0 — Unblock (week 0, parallel, non-engineering)
| Action | Owner | Output |
|---|---|---|
| Email NessPRO, ParaDocs, fme: non-prod `/dctm-rest` access under their control, intro to OpenText Israel AE for a PoC licence, co-development interest | Founders | One committed lab contact |
| Pick one Documentum-running prospect; send the §2b checklist from the lab doc | Sales | Design partner |
| Submit OpenText Partner Program application (Technology track); ask PAM about the Demonstrator module, the development-tool clause, `registry.opentext.com` access | Founders | Application in flight |
| Ask any contact for the REST SDK's OpenAPI spec (23.4+) | Eng | Spec for contract tests |

## Phase 1 — Build against a mock (weeks 1–3)
1. **Mock server** (own implementation, Elastic-licensed fixture used as reference only): `/repositories`, `/repositories/{repo}`, `/cabinets`, `/folders/{id}/folders`, `/folders/{id}/documents?inline=true`, `/objects/{id}`, `primary-content` → `enclosure` bytes, `?dql=`, `/search?q=`, `/types`, `/currentuser`; paging (`page`, `items-per-page`, `next` link), 401/403/404/429; Basic and Bearer accepted. Ship as a `backend/tests/integrations` container target.
2. **Client** `documentum_client.py`: session with `Accept: application/vnd.emc.documentum+json`; `test_connection`, `list_files` (DQL `FOLDER(…, DESCEND)` per page), `read_file` / `read_raw_bytes` (size check before download, `media-url-policy=local`, `NamedBytes` with format→MIME map from `/formats`), `search_files` (`/search?q=` + live `object_name`/`title` LIKE), `get_schemas` with `index_mode` tiers, `prompt_schema`. File id = `r_object_id`, path carried for display; scope enforced at the resolve chokepoint (`root_path` + `include_globs`).
3. **Schemas + registry**: `DocumentumConfig` (`rest_url`, `repository`, `root_path`, `include_globs`, `recursive`, `index_mode`, `max_file_mb`, `max_catalog_objects`, `object_types`, `current_versions_only`, `allow_http`); credentials `userpass` (system+user) and `service`; registry entry `category="files"`, `catalog_ownership="shared"`, `requires_license="enterprise"`, explicit `client_path`, `dev_only=True`. Icon + `DataSourceIcon.vue` mapping.
4. **Tests**: unit tests at the HTTP boundary (`responses`/`respx`) per `backend/tests/AGENTS.md`; integration run against the mock.

## Phase 2 — Per-user auth (weeks 3–5, can start before a lab)
1. `otds_impersonation` `AuthVariant(overlay=True, scopes=["user"])`: system creds = OTDS URL, client id/secret, partition; user overlay = `documentum_login` (default BOW email/UPN). Token exchange call, per-user token cache ≤ `expires_in`, fail-closed on unknown user.
2. Email → `userid@partition` resolver (`/otdsws/rest/users`, fallback DQL on `user_address`), admin default rule (`email` / `upn` / `sam@partition`), per-user override.
3. `oauth` variant: `documentum` branch in `get_oauth_params` (authorize/token URLs from the connection's OTDS URL), `OAuthDelegatedCredentials`, `default_user_auth_modes` returning `["oauth"]` when an OAuth client is configured.
4. UI copy per variant stating whose identity Documentum sees; docs page `docs/documentum.md` modeled on `docs/sharepoint-server.md`.

## Phase 3 — Real-instance validation (as soon as any lab exists; 1–2 weeks)
Checklist to close, recorded as anonymised fixtures:
- REST version and `rest.security.auth.mode`; page-size defaults (`rest.paging.default.size`/`max.size`).
- Basic, Bearer (`Authorization` header vs `access_token=`), `OTDS_TICKET` header acceptance.
- **Token-exchange token accepted by `/dctm-rest`** (auth doc §7.1) and ACL trimming with two users of different rights.
- Content links: `media-url-policy=local` vs ACS redirects; renditions; zero-size external-store objects.
- xPlore present/absent behaviour of `/search`; DQL `RETURN_TOP`/`RETURN_RANGE` vs REST paging.
- Custom subtypes, repeating attributes, multi-filed objects, versions (`r_object_id` vs `i_chronicle_id`).
- Rate limiting (429 `Retry-After`), DFC session limits.
Exit: remove `dev_only`, publish the verification boundary, CHANGELOG entry per the release-notes skill.

## Phase 4 — Own lab and hardening (months 2–4, after partner entitlement)
- Pull `dctm-server`, `dctm-rest`, `dctm-admin`, xPlore from `registry.opentext.com`; adapt the community compose/K8s recipes; add a `CONTAINER_REGISTRY` integration target so CI runs the real thing.
- Then: `ticketforuser` fallback transport, Kerberos service variant only if a customer asks, optional "objects"-shaped DQL mode for metadata questions (SOP counts by state, etc.).

## Dependencies and risks
| Risk | Mitigation |
|---|---|
| No lab materialises | Phases 1–2 proceed on the mock; ship `dev_only`; course 3-8010 (USD 4,500) buys a developer 20 lab hours if nothing else lands within a month |
| Impersonation not accepted by REST on a given build | `ticketforuser` transport, then `oauth` variant |
| Login-name convention differs per tenant | Resolver + per-user override; fail closed |
| Customers on legacy (7.x/16.x) REST | Target 20.x+ for support statements; REST WAR is backwards-compatible per community |
| Unofficial images tempting during Phase 1 | Rejected on EULA grounds; mock only |

## Estimate
Engineering is roughly **one week**, not six: the comparable `sharepoint_onprem` connector is a
445-line client plus a 254-line unit test and landed in one commit. Client + schemas + registry
+ tests ≈ 1–2 days; mock ≈ 0.5 day; OTDS impersonation overlay ≈ 1–2 days; OAuth branch ≈ 0.5
day; docs ≈ 0.5 day. Real-instance validation ≈ 1–2 days once access exists. Elapsed time is
dominated entirely by Phase 0 (lab access), which is a waiting problem, not a building one.
Build everything on the mock in week 1, ship `dev_only`, flip live the day a repository appears.
