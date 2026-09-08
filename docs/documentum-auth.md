# Documentum Connector — Identity and Per-User Authentication

**Status:** Research / design only — no implementation.
**Companion to:** [`documentum-connector-analysis.md`](documentum-connector-analysis.md) (§4 there lists REST
auth modes) and [`documentum-lab-access.md`](documentum-lab-access.md).
**Researched:** 2026-09-08. Question answered here: *customers keep their users in LDAP/AD or
Entra ID — how does a BOW user get to Documentum as themselves, so Documentum's ACLs do the
security trimming?* Every claim carries a source; §7 lists what needs a lab to confirm.

---

## 0. Bottom line up front

1. **Since Documentum 23.4, Content Server no longer authenticates LDAP or Kerberos users
   itself.** Every non-inline login is brokered by **OTDS (OpenText Directory Services)**:
   Content Server checks that `user_login_name` exists, then hands the credential to the
   `OTDSAuthentication` bridge, which asks OTDS. The old LDAP/Kerberos/Netegrity plugins are
   "deprecated (disabled completely)". From 24.4, OTDS is also the licence gate: a user not in a
   licensed OTDS partition cannot log in at all (only `dmadmin` is exempt). So "LDAP" and
   "Entra" both mean **OTDS** on any current release.

2. **OTDS is a standard-ish OAuth2/OIDC authorization server.** It has token, auth and JWKS
   endpoints, confidential OAuth clients, and, crucially, **impersonation via RFC 8693 token
   exchange**: a confidential client with "Allow impersonation" can mint an access token *for
   any user* (`subject_token=<userid>@<partition>`). OpenText's own Python library `pyxecm`
   does exactly this. Documentum REST accepts OTDS tokens (`otds_token` / `oauth2` modes) and
   opens the DFC session **as that user**, so repository ACLs apply on every call.

3. **Recommendation for BOW, in order:**
   - **Primary: OTDS impersonation as an "identity overlay"** (the pattern the Qlik connector
     already uses). The admin stores one confidential OTDS client (id + secret) on the
     connection; each BOW user contributes only their Documentum login identity (defaulting to
     their BOW email/UPN). No per-user password, works for LDAP-synced *and* Entra-only users,
     supports MFA-only tenants, no browser round-trip. Needs the customer to flip two flags in
     OTDS. One lab test outstanding: the exchanged token used against `/dctm-rest` (proven for
     Content Server, strongly evidenced for Documentum).
   - **Secondary: per-user OTDS OAuth2 authorization-code sign-in** through BOW's existing
     delegated-OAuth machinery (`connection_oauth_service.get_oauth_params`). The user is
     redirected to OTDS, which redirects to Entra/AD-FS/Okta; BOW stores the user's OTDS token.
     For tenants that refuse impersonation.
   - **Fallback: per-user username/password** (the SharePoint Server "User-required NTLM"
     UX). Works only where OTDS can validate a password (AD-synchronized partition or
     OTDS-stored password). **Does not work for Entra-only users** and bypasses MFA.
   - **Always available: shared service account** (Basic or OTDS client-credentials). Every
     BOW user sees what the account sees; flag it in the UI exactly as we do for Kerberos
     service mode on SharePoint Server.

4. **Entra on-behalf-of does not reach Documentum.** BOW already does Entra OBO for
   Microsoft resources (Power BI, SharePoint). Documentum REST rejects raw Entra tokens; the
   audience it trusts is OTDS. The closest Entra-native path is exchanging the user's Entra
   `id_token` for an OTDS ticket (`POST /otdsws/rest/authentication/token`), which works only
   when the customer's OTDS has an OIDC handler for *our* Entra app. Impersonation is simpler
   and does not depend on which IdP sits behind OTDS.

5. **Identity mapping is the real risk.** Documentum's join key is `user_login_name`, which
   OTDS fills from `oTExternalID3` (UPN, `user@domain`) by default, but customers override it
   (`synced_user_login_name=sAMAccountName`). Impersonation needs `userid@partition`, not an
   email. The connector must resolve BOW's email/UPN → OTDS user (`/otdsws/rest/users` search
   or a repository lookup on `user_address`/`user_login_name`) and must expose a per-user
   "Documentum login" override for tenants where the two differ.

---

## 1. How Documentum authenticates users today

| Mechanism | Pre-23.4 | 23.4 / 24.4+ (OTDS mandatory) |
|---|---|---|
| Inline password (`user_source='inline password'`) | Local check | Still local, **but** from 24.4 the user must also hold an OTDS licence (`MigrateInlineUsersToOtds` moves inline users into a non-synchronized partition). `dmadmin` exempt. |
| LDAP (`user_source='LDAP'`, `dm_ldap_config`, `dm_LDAPSynchronization` job) | Content Server validated the password against LDAP | **Plugin disabled.** For `LDAP` or `OTDS` sourced users "the Repository will verify if the user_login_name exists. If yes, it will send the Authentication request to the JMS, which will contact the OTDS." Direct password logins to an `LDAP`-sourced user via DA/iAPI fail. |
| Kerberos (`dm_krb`), Netegrity, RSA plugins | Plugins | Deprecated/disabled; Kerberos now lives in OTDS as an authentication handler (unverified per-mechanism). |
| Login tickets (`DM_TICKET`, superuser mints for any user) | Yes | Still alive: D2's OTDS SSO ends by requesting a `dm_ticket`; REST 25.4's `AuthType` still lists `LOGIN_TICKET`. |
| DFC principal mode (privileged client, no password) | Yes | Still listed (`PRINCIPAL`) — used by REST "pre-authenticated" mode. |
| OTDS | Optional (16.x–23.2) | **Mandatory** (23.4 auth, 24.4 licensing). Bridge = `OTDSAuthentication` web app on the JMS; 25.4 moved it to a standalone `OTDSAuthLicenseHttpServer` on port 8400. |

The bridge is configured in `otdsauth.properties`: `otds_rest_credential_url`, `otds_rest_ticket_url`,
`otds_rest_oauth2_url`, `passauth_use_oauth2_token`, `client_id`, `client_secret`,
`<repo>_resource_id`, `<repo>_secretKey`, `synced_user_login_name`, `cert_jwks_url`. At the
DFC level the "password" carries a prefix telling the bridge what it is: `dm_otds_password=<pw>`,
`dm_otds_oauth=<access_token>`, `dm_otds_ticket=<ticket>`. Sources: dbi-services "Login through
OTDS without oTExternalID3" and "Managing licenses through OTDS", aldago "OTDS FAQ" and "24.4
OTDS licensing", appworks-tips Documentum store connector, contentobserver OIDC guide.

**Consequence for us:** password-based access only works where OTDS can validate a password;
everything else must present something OTDS issued.

---

## 2. OTDS as identity broker: the pieces we depend on

- **Partitions.** *Synchronized* (pulled from AD/LDAP; passwords validated against AD) and
  *non-synchronized* (manually created, SCIM-pushed from Entra, or migrated inline users).
  Every user carries `oTExternalID1` (`userid`), `oTExternalID2` (`userid@partition`),
  `oTExternalID3` (UPN `user@domain`, the default login mapping), `oTExternalID4` (`DOMAIN\user`).
- **Authentication handlers.** LDAP/AD password, Kerberos/IWA, **OIDC** (Entra, Okta…), **SAML
  2.0**. Each handler maps an IdP claim ("User Identifier Field", e.g. `userPrincipalName`) onto
  an OTDS attribute ("Authentication principal attribute", default `oTExternalID3`).
- **The repository as an OTDS resource.** A REST resource pointing at the Content Server's
  `/dmotdsrest`, with user/group synchronization and "Create and modify users" on. Consolidation
  pushes users into `dm_user` with `user_source='OTDS'`; default mapping
  `user_login_name << oTExternalID3`, `user_name << displayName`, `user_address << mail`.
  **There is no JIT creation in the repository at login time**: the user must already exist
  (consolidated) or login fails at the `user_login_name` check.
- **OAuth clients.** Created in OTDS Admin. Fields visible through the REST payload:
  `confidential`, `secret`, `redirectURLs`, `allowImpersonation`, `impersonateList`,
  `allowedScopes`, `authScopes` (partition scope), `accessTokenLifeTime` (default 3600 s),
  `refreshTokenLifeTime`, `allowRefreshToken`. Resources carry their own
  `allowImpersonation`/`impersonateList` (`PUT /otdsws/rest/resources/<name>/impersonation`).
- **Endpoints.** `/otdsws/oauth2/token`, `/otdsws/oauth2/auth`, `/otdsws/oauth2/jwks`,
  `/otdsws/login` (also acts as token endpoint), `/otdsws/rest/authentication/credentials`,
  `/otdsws/rest/authentication/ticketforuser`, `/otdsws/rest/authentication/oauth/tokeninfo`
  (401 = no access to resource, 410 = expired/revoked), `/otdsws/rest/users`. Tokens are RS256
  JWTs. Grants observed working: `password`, `client_credentials`, implicit, authorization code
  (D2-REST uses it), `refresh_token`, and **`urn:ietf:params:oauth:grant-type:token-exchange`**.
  Scope `resource:<Resource Name>` binds a token to one resource.

Sources: `opentext/pyxecm` `otds.py` (OpenText's own library, Apache-2.0), forums 308065 /
312806 / 313276 / 292509, Microsoft Learn "OpenText Directory Services provisioning" and
"Directory Services SSO", dbi-services Azure/sAMAccountName post, contentobserver OIDC guide.

---

## 3. Entra ID specifically

| How customers wire Entra to Documentum | Notes |
|---|---|
| **OTDS OIDC handler** (most common) | Entra app registration with redirect `https://<otds>/otdsws/login?authhandler=<Name>`; identifier `userPrincipalName` (Azure does not expose `sAMAccountName`); dbi maps it onto a custom OTDS attribute when the partition is keyed on `sAMAccountName`. Clients trigger it with `…/otdsws/login?authhandler=OIDC&response_type=token&client_id=…`. |
| **SAML** via the Entra gallery app "Directory Services" | Reply URL `https://<otds>/otdsws/login`; JIT user creation in OTDS on by default. |
| **Provisioning** | Either the on-prem AD (source of truth for hybrid Entra) is synchronized into an OTDS partition, or Entra pushes users by **SCIM** into a non-synchronized partition. **Entra provisioning never sends passwords.** |
| Documentum-native, no OTDS | Legacy: REST `saml`/`saml-basic` modes (xCP 16.4 era, `SAMLAuthentication.war` on the JMS) and REST "pre-authenticated" mode behind a proxy that validates the Entra JWT. Survival after 23.4 unverified. **Raw Entra access tokens are not accepted by `/dctm-rest`.** |

**Implication:** an Entra-only user (SCIM/JIT, no synchronized AD) has **no password OTDS can
check**, so per-user Basic auth cannot work for them; only OTDS token flows can. Hybrid
tenants (AD synced to OTDS, Entra federated) can still do password auth, at the cost of
bypassing Entra MFA and Conditional Access.

---

## 4. Options for BOW, mapped to what the codebase already has

BOW today: per-user credentials on `user_required` connections; delegated OAuth with
per-type provider constants (`connection_oauth_service.get_oauth_params`, PKCE, refresh);
Entra on-behalf-of for Microsoft resources (`exchange_obo_token`, `ENTRA_OBO_CONNECTION_TYPES`);
Kerberos constrained delegation for SQL Server (`kerberos_delegated`); and the Qlik **identity
overlay** (`AuthVariant(overlay=True)`: admin keeps the heavy secret, user supplies identity
fields only, merged at resolve time). LDAP is a *login* method for BOW; we never store LDAP
passwords.

| # | Option | Flow | Customer prerequisites | LDAP/AD-synced users | Entra-only users | ACLs | BOW mechanism | Verdict |
|---|---|---|---|---|---|---|---|---|
| 1 | **OTDS impersonation (token exchange)** | Backend: `POST /otdsws/oauth2/token` with `grant_type=urn:ietf:params:oauth:grant-type:token-exchange`, `client_id`, `client_secret`, `subject_token=<userid>@<partition>`, `subject_token_type=urn:opentext.com:oauth:string:user_id`, `requested_token_type=urn:ietf:params:oauth:token-type:access_token` → Bearer for the user → `/dctm-rest` with `Authorization: Bearer` (or `access_token=`) | REST in an `*otds_token*`/`oauth2` mode (D2 shops already run `ct-otds_ticket-otds_token`); a confidential OTDS OAuth client with **Allow impersonation**, scoped to the users' partition; **impersonation enabled on the repository resource**; bridge with `cert_jwks_url` | Yes | **Yes** | Full — DFC session is the user's | New `otds_impersonation` `AuthVariant(overlay=True, scopes=["user"])`: system creds hold client id/secret + OTDS URL + partition; user overlay holds `documentum_login` (default: BOW email). Token cached per user ≤ `expires_in`. | **Primary.** Same shape as `pyxecm.impersonate_user`. One lab test needed (§7.1). |
| 2 | OTDS `ticketforuser` | Service account's OTDS ticket → `POST /otdsws/rest/authentication/ticketforuser {userName, ticket}` (or `…/resource/ticketforuser`) → `OTDS_TICKET` header (`otds_ticket-otds_token` mode) | Same impersonation flags; a service user in OTDS | Yes | Yes | Full | Same overlay variant, alternate transport | Fallback for OTDS builds where token exchange misbehaves. Endpoint was called "internal" in 2017, used in production since. |
| 3 | **Per-user OTDS OAuth2 sign-in** (authorization code) | Browser: BOW → `…/otdsws/oauth2/auth` (optionally `?authhandler=<Entra>`) → IdP → back to BOW with code → token + refresh | OAuth client with BOW's redirect URI; `allowRefreshToken`; REST in `oauth2`/`otds_token` mode | Yes | Yes | Full | Add a `documentum` branch to `get_oauth_params` (authorize/token URLs from the connection's `otds_url`; client id/secret from admin creds) and `OAuthDelegatedCredentials` variant, exactly like ServiceNow/Monday | Secondary, for tenants that forbid impersonation. PKCE/discovery on on-prem OTDS unverified. |
| 4 | Entra `id_token` → OTDS ticket | BOW's Entra login `id_token` → `POST /otdsws/rest/authentication/token {tokenBinary: base64(id_token)}` → OTDS ticket | OTDS OIDC handler trusting **BOW's** Entra app (audience) | Yes | Yes | Full | Would reuse the Entra login token BOW already holds (as the Power BI OBO path does) | Niche; finicky audience handling; only when the customer's OTDS already federates with the same Entra tenant and will add our app. |
| 5 | Per-user username/password (Basic / `otds_password`) | User types Documentum credentials into BOW; Basic on every call | REST allows `basic`; OTDS can validate the password (AD-synchronized partition or OTDS password) | Yes | **No** | Full | Existing `userpass` `AuthVariant(scopes=["system","user"])` — identical to SharePoint Server NTLM | Fallback; bypasses MFA; refuse for Entra-only tenants. |
| 6 | Superuser mints `DM_TICKET` for the user | Needs DFC/iAPI (Java) or a JMS servlet with superuser creds | Java footprint on our side or customer's | Yes | Yes | Full | None | No — DFC footprint and a superuser secret in BOW. |
| 7 | REST pre-authenticated / principal mode | Customer's proxy validates BOW's assertion, sets a user header; REST uses privileged DFC | Custom filter deployed on the REST tier | Yes | Yes | Full | None | No — customer-side custom deployment; keep as a talking point only. |
| 8 | Shared service account + BOW-side ACL evaluation | Read-all account; export ACLs and trim in BOW | Read-all account | n/a | n/a | **Only if we re-implement Documentum ACLs** | — | No for trimming. Shared account **without** trimming remains the simple system-scope option, clearly labelled. |
| 9 | Entra OBO (as for Power BI) | `exchange_obo_token` against `login.microsoftonline.com` | — | — | — | — | Exists, but Documentum trusts OTDS tokens, not Entra tokens | **Not applicable.** |

### 4a. Identity mapping (applies to options 1–4)
- Repository identity fields: `user_name` (unique display), `user_login_name` (presented at
  login), `user_os_name`, `user_address` (email), `user_login_domain`, `user_ldap_dn`,
  `user_global_unique_id` (AD `objectGUID`), `user_source`.
- OTDS default: `user_login_name = oTExternalID3` (UPN). Overrides are common
  (`synced_user_login_name=sAMAccountName`). Case mismatches between `user_name` and
  `user_login_name` are a known breaker of OTDS's `user_source='OTDS'` lookup.
- Impersonation wants `userid@partition`. Resolve BOW's email/UPN → OTDS user via
  `GET /otdsws/rest/users?where_…` (needs a client/service identity with read rights) or, once
  a service session exists, via DQL `SELECT user_login_name FROM dm_user WHERE user_address='…'`.
  Cache the mapping; expose a per-user override field ("Documentum login") and an admin-level
  default rule (`email`, `upn`, `samaccountname@partition`).
- If the user does not exist in the repository, fail closed with a clear message ("not
  provisioned in Documentum"); never fall back to the service account for that user.

### 4b. Customer-side checklist for the primary option
1. REST `rest.security.auth.mode` includes an OTDS token scheme (`otds_token`, `oauth2`, or
   `ct-otds_ticket-otds_token`); `rest.security.otds.login.url` and `rest.security.realm.name` set.
2. OTDS: create confidential OAuth client "BOW" — Allow impersonation on, partition scope =
   users' partition(s), no redirect URI needed for option 1 (add BOW's callback for option 3).
3. OTDS: repository resource → Impersonation → allow, optionally `impersonateList` = the BOW
   client.
4. Bridge (`otdsauth.properties`): `cert_jwks_url`/`certificate`, `synced_user_login_name` if
   the login name is not UPN.
5. Tell us the partition name(s) and the login-name convention.
6. Security review talking points: BOW holds one client secret (encrypted at rest), every call
   runs as the end user (Documentum audit trail shows the real user), tokens live ≤ 1 h, no
   passwords stored, MFA/Conditional Access remain enforced at the IdP for the user's own
   logins (impersonation itself is a server-to-server trust the customer grants explicitly).

---

## 5. How other integrators do it (for calibration)

| Integrator | Documentum auth | Per-user? |
|---|---|---|
| Elastic connector, OneTeg, Microsoft SharePoint Documentum connector, BA Insight, fme Copilot connector | Privileged crawl/service account; ACLs exported and mapped to AD/Entra identities offline for search-time trimming | Trimming, not on-behalf-of |
| One Fox (Power Automate) | Documentum username + password per connection | Per connection |
| ServiceNow (Reva) Documentum/D2 connector | "OTDS REST API, DCTM REST API and D2 Smartview … configured with OTDS" | OTDS tokens/tickets (impersonation not stated) |
| OpenText `pyxecm`, OTCS ecosystem | OTDS client-credentials + **`impersonate_user`** (token exchange or `ticketforuser`) | **Yes** |

No public Documentum connector documents OTDS impersonation; the Content Server ecosystem and
OpenText's own library use it routinely. Live per-user ACL enforcement is the differentiator
worth having.

---

## 6. Recommended build sequence
1. `userpass` (system + user scopes) and `service` (OTDS client-credentials) variants — trivial,
   cover legacy and hybrid tenants, and every lab we are likely to get first.
2. `otds_impersonation` overlay variant with the email → `userid@partition` resolver and
   per-user override; token cache; fail-closed on unknown users.
3. `oauth` delegated variant via `get_oauth_params` for impersonation-averse tenants.
4. UI copy per variant stating exactly whose identity Documentum sees (as
   `docs/sharepoint-server.md` does), plus a "verification boundary" note until §7 is closed.

---

## 7. Needs a lab to confirm (or an OpenText answer)
1. **Token-exchange token accepted by `/dctm-rest`.** Verified for Content Server (forum
   312806, pyxecm) and REST is shown accepting OTDS OAuth tokens and needing resource
   impersonation enabled (appworks-tips), but no public source shows the *exchanged* token used
   against Documentum REST. First thing to test.
2. `Authorization: Bearer` vs `access_token=` query parameter acceptance per auth mode; which
   modes accept the `OTDS_TICKET` header.
3. Semantics of `oauth2`/`ct-oauth2` vs `otds_token` modes (can `oauth2` trust a non-OTDS
   issuer? almost certainly not).
4. PKCE and `.well-known/openid-configuration` on customer-hosted OTDS (documented for
   OpenText's cloud platform only).
5. Fate of `dm_krb` Kerberos users and native REST SAML after 23.4; `dm_LDAPSynchronization`
   support status alongside OTDS consolidation.
6. Official support statement for `ticketforuser` (called internal in 2017).
7. OTDS user-search authorization: which rights the BOW client needs to resolve emails.

---

## 8. Sources
- dbi-services: https://www.dbi-services.com/blog/documentum-login-through-otds-without-otexternalid3/ ; https://www.dbi-services.com/blog/dctm-managing-licenses-through-otds/ ; https://www.dbi-services.com/blog/dctm-otds-sso-with-samaccountname-ad-ldap-attribute-and-azure/ ; https://www.dbi-services.com/blog/dctm-d2-sso-through-otds-fails-with-dm_license_e_invalid_license/ ; https://www.dbi-services.com/blog/dctm-incorrect-r_object_id-reference-in-otds/ ; https://www.dbi-services.com/blog/iapi-login-with-a-dm_ticket-for-a-specific-user/ ; https://www.dbi-services.com/blog/dctm-mfa-non-mfa-within-otds-based-on-target-applications/
- aldago: https://blog.aldago.es/2025/02/16/otds-faq-for-documentum/ ; https://blog.aldago.es/2025/05/19/documentum-24-4-otds-licensing-configuration/ ; https://blog.aldago.es/2024/03/06/d2-config-2fa-otds-integration/ ; https://blog.aldago.es/2026/01/13/moving-documentum-otdsauthenticator-outside-jms/
- appworks-tips (OTDS + REST + `otdsauth.properties`): https://appworks-tips.com/2021/10/01/113_bring_life_in_the_documentum_connector/
- contentobserver (OIDC for Documentum via OTDS): https://contentobserver.wordpress.com/2023/06/20/how-to-set-up-openid-connect-oidc-authentication-for-documentum-client-applications-using-otds/
- OpenText `pyxecm` `otds.py` (OAuth client fields, token exchange, `ticketforuser`, resource impersonation): https://github.com/opentext/pyxecm/blob/main/packages/pyxecm/src/pyxecm/otds.py
- REST 25.4 javadocs (`AuthType`, `DefaultSecurityRuntime`): https://github.com/opentext/d2sv-sdk/blob/main/25.4.0/dctm-rest/com/emc/documentum/rest/dfc/AuthType.html ; D2FS REST guide 23.4 (auth-mode list): https://opentext.github.io/d2sv-sdk/23.4.0/bundle/pdf/OpenText%20Documentum%20D2FS%20REST%20Services%20Development%20Guide.pdf
- Forums (forums.opentext.com/forums/developer/discussion/…): 312806 (client-credentials + token exchange worked example), 308065 (OTDS OAuth2 walkthrough, tokeninfo), 313276, 292509, 300099, 235842 (`ticketforuser` "internal", 2017), 311729 (`ct-otds_ticket-otds_token` order, OpenText staff), 303765 (Entra token not accepted by REST; pre-auth mode), 312650 (pre-authenticated mode), 296840 (id_token → OTDS ticket), 310101 (email → OTDS user)
- Microsoft Learn: https://learn.microsoft.com/en-us/entra/identity/saas-apps/open-text-directory-services-provisioning-tutorial ; https://learn.microsoft.com/en-us/entra/identity/saas-apps/directory-services-tutorial ; https://learn.microsoft.com/en-us/entra/identity/app-provisioning/known-issues ; https://learn.microsoft.com/en-us/sharepoint/search/configure-and-use-the-documentum-connector
- Integrators: https://store.servicenow.com/store/app/afbb27ea1b246a50a85b16db234bcb5c ; https://learn.microsoft.com/en-us/connectors/opentextdocumentum/ ; https://www.elastic.co/docs/reference/search-connectors/es-connectors-opentext ; https://help.uplandsoftware.com/bai/ConnectivityHub22/Secure_content/Security_Model.htm ; https://en.fme.de/digital-transformation/artificial-intelligence/integration-of-opentext-documentum-into-microsoft-365-copilot/
- BOW internals referenced: `backend/app/services/connection_oauth_service.py` (`get_oauth_params`, `exchange_obo_token`), `backend/app/services/connection_service.py` (`default_user_auth_modes`), `backend/app/schemas/data_source_registry.py` (`AuthVariant.overlay`), `docs/ldap-integration.md`, `docs/oauth-apps.md`, `docs/sharepoint-server.md`.
