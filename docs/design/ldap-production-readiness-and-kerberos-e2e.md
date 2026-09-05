# LDAP production readiness and LDAP-to-SQL Kerberos verification

**Status: proposed implementation and verification plan — not implemented or tested by this document.**

Created: 2026-09-05. Code-review baseline: `c471b14a`.

## 1. Objective and completion boundary

Make BOW's LDAP login and directory synchronization suitable for a defined,
supported production deployment, then prove the complete flow in the existing
isolated AWS lab:

```text
Browser user submits directory credentials over HTTPS
  → BOW validates the directory's TLS identity
  → LDAP lookup and user bind authenticate a unique directory object
  → BOW establishes a session linked to that trusted directory identity
  → BOW checks organization and connection permissions
  → trusted server-side mapping selects that user's Kerberos principal
  → service keytab obtains constrained-delegation credentials
  → SQL Server authenticates that user and enforces that user's permissions
  → BOW returns only authorized results to that user's session
```

LDAP login does **not** itself produce a Kerberos ticket. LDAP authentication,
BOW authorization, identity mapping, and Kerberos delegation are separate trust
boundaries. An email-shaped string, a successful bind, a green connection badge,
or SQL reporting `KERBEROS` alone does not prove the full chain.

This document authorizes no implementation, infrastructure changes, tests,
credential transfers, commits, or deployments. Those are future execution steps.
All proposed settings and data-model changes below are design requirements, not
claims that corresponding configuration fields already exist.

### Scope

- LDAP/AD authentication, transport security, admission, provisioning, group
  synchronization, revocation, operational behavior, and regression coverage.
- Trusted LDAP identity → SQL Server delegated identity through BOW's actual
  session, connection service, UI, query execution, and prompt flow.
- Both delegated users and the separately configured service-account mode.
- Deployment-neutral packaging: Docker/Kubernetes/Helm as applicable, not an
  OpenShift requirement.

### Not certified by this work

- Entra/OIDC/SAML login, cross-forest delegation, or arbitrary LDAP products not
  included in the supported-server matrix.
- Per-user SharePoint delegation. The existing SharePoint connector's
  service-account Kerberos path is a separate capability.
- Universal immunity to every native-driver interleaving or a comprehensive
  independent penetration test. Publish exactly which versions and cases pass.

## 2. Starting evidence: what is and is not known

The earlier isolated AD/SQL lab demonstrated service Kerberos authentication,
sequential delegated identities, and distinct SQL permissions. It exposed a
cold-start concurrent identity mismatch. The minimal repair moved engine
initialization inside credential-cache activation and was ported in `c471b14a`.
The before/after evidence and local regression coverage are recorded in
[the engine-activation feedback loop](../feedback-loops/mssql-kerberos-engine-activation.md).

That evidence does **not** validate LDAP login, LDAP-to-principal mapping, or
browser/prompt execution through LDAP-authenticated BOW sessions. Directly
supplying synthetic principals to a connector bypasses those layers.

The findings below are based on source review, not live exploit verification:

| ID | Observed implementation / source | Required outcome |
| --- | --- | --- |
| L1 | `backend/app/ee/ldap/connection.py::_build_server` uses `ssl.CERT_NONE`. | Validate the directory certificate chain and hostname; support a deployment-owned private CA. |
| L2 | `get_connection` binds before `start_tls`; `bind_user` does not perform StartTLS. | No simple-bind credentials sent before verified TLS on either path. |
| L3 | `find_user_dn` interpolates login input directly into a search filter. | Escape filter values and require a unique, unambiguous directory identity. |
| L4 | `backend/app/core/auth.py::_ldap_authenticate` classifies search exceptions broadly; `_do_authenticate` permits local-password fallback on `unreachable`. | Typed failure handling and explicit, restricted break-glass policy; no accidental fallback. |
| L5 | `authenticate` applies the local-login restriction after successful LDAP auth in `sso_only` mode. | Authentication provenance distinguishes directory login from local-password login. |
| L6 | User/group searches set `paged_size` but do not consume subsequent page cookies. | Complete, validated directory snapshots before destructive reconciliation. |
| L7 | `sync_service.py::_cleanup_org_memberships` infers ownership from remaining group membership; `jobs.py` applies global LDAP settings to all organizations. | Explicit provisioning ownership and organization scope; protect unrelated memberships. |
| L8 | `connection_identity.py::resolve_kerberos_principal` prefers a member-saved principal, then an email-shaped login. | Privileged delegation uses only an administrator-approved, directory-verified mapping. |

Existing LDAP API tests in `backend/tests/e2e/test_ldap.py` substitute connection
or sync components. They do not certify real bind order, certificate validation,
complete pagination, or end-to-end directory identity.

## 3. Decisions to settle before implementation

Recommended defaults below are proposals. Record the final decisions and support
boundaries before changing behavior for existing deployments.

| Decision | Proposed production policy |
| --- | --- |
| Supported directory | AD first, with exact Windows/AD and client-library versions recorded. Test generic LDAP separately before advertising it. |
| Transport | Verified LDAPS by default. StartTLS supported only with upgrade-before-bind and no downgrade. Cleartext simple bind prohibited in production. |
| Directory outage | Directory users fail closed. A separately provisioned local break-glass administrator may use an explicit audited path; no fallback for normal users. |
| Admission | Explicit organization-to-directory/group scope. Being present in AD alone grants no BOW organization access. |
| Identity key | Provider instance + stable directory object identity, such as AD objectGUID/objectSID; email and UPN are mutable attributes. |
| Principal mapping | Authoritative directory UPN/realm or an audited administrator mapping linked to the stable object. Never a member-supplied impersonation target. |
| Group semantics | Document direct vs nested membership, supported depth/cycle handling, primary-group behavior, and AD ranged membership retrieval. Reject unsupported configurations. |
| Revocation | Deny new logins immediately when the live directory check rejects the account. Proposed maximum existing-session access revocation: five minutes after directory/group removal becomes observable. Define behavior during directory outages and AD replication delay. |
| Existing local accounts | Explicit safe linking/migration; do not silently attach an LDAP identity to a privileged local account merely because the emails match. |
| Session types | Define directory, local break-glass, API-token, automation, and delegated-connection policies separately. Local admin login must not automatically grant AD impersonation. |

If an intended customer requirement differs, adjust the specification and test
expectations together. Do not silently weaken security to make a lab pass.

## 4. Implementation work packages

### A. Transport and bind lifecycle — release blocking

- Validate CA chain, hostname, expiry, and acceptable TLS versions for both the
  lookup-account connection and user bind. Mount private CA trust from deployment
  configuration, not a file path supplied through a normal user's form.
- Validate URL, port, SSL/StartTLS combinations and require explicit directory
  destinations. Define referral handling; do not forward bind credentials to
  unapproved referral targets.
- Use explicit verified TLS-before-bind sequencing. If TLS negotiation fails,
  stop without binding or retrying over cleartext.
- Reject empty passwords before network activity; do not interpret an anonymous
  or unauthenticated bind as a user login.
- Close connections on all success, failure, timeout, and cancellation paths.
- Bound connect, receive, and total operation time; avoid blocking async request
  workers with unbounded synchronous LDAP operations.

### B. Lookup, login policy, and safe provisioning — release blocking

- Escape user input with LDAP filter-value escaping; validate configurable
  attribute names and trusted administrator-authored filter templates separately.
- Handle no match and multiple matches explicitly. A one-result limit is not
  evidence of uniqueness. Authenticate the exact object that was resolved.
- Distinguish invalid credentials, admission denial, ambiguous identity, TLS
  failure, timeout, directory outage, and malformed configuration internally.
  Return non-enumerating login errors; retain sanitized operational reason codes.
- Preserve authentication provenance through the login path so `sso_only`
  accepts permitted directory users without allowing ordinary local-password
  bypass. Make any break-glass mechanism explicit, restricted, and audited.
- Define disabled, locked, expired, password-expired, and password-change-required
  account behavior. Do not bypass directory rejection or invent a successful login.
- Apply login throttling without aggressive retries that could lock AD accounts.
- Provision non-admin accounts only after successful authentication and
  organization admission; enforce seat limits and existing role policies.
- Link accounts using trusted identity ownership, not arbitrary email equality.
  Test email/UPN renames, object recreation, duplicates, and privileged local
  account collisions before migrating existing accounts.

### C. Complete and non-destructive group synchronization — release blocking

- Consume every page cookie for users and groups. Handle repeated cookies,
  size limits, missing attributes, AD ranged `member` values, referral failures,
  and partial searches explicitly.
- Separate fetch/validate from reconciliation. A partial or failed snapshot must
  never be treated as an authoritative empty directory.
- Record membership/role provenance. Remove only grants owned by this directory
  integration; preserve manual, invited, SCIM/OIDC, and other-provider grants.
- Bind each organization to an explicit directory/group scope. A global directory
  setting must not implicitly enroll the same users into unrelated organizations.
- Make previews and real sync use the same identity-resolution and scope rules.
  Preview must include consequential organization-membership removals.
- Serialize overlapping manual/background syncs per scope, make reconciliation
  idempotent, and roll back on failure. Avoid commits based on stale snapshots.
- Define nested-group and DN normalization behavior. Do not substitute broad
  privileges when a group member cannot be resolved.
- Propagate revocation to authorization caches, sessions, connection overlays,
  and applicable scheduled work within the approved bound. Track sync freshness;
  a stale status must not be reported as successful enforcement.

### D. Trusted directory-to-Kerberos identity — release blocking for SQL SSO

- Persist directory authentication provenance, stable identity, authoritative
  UPN, provider scope, and last verification state server-side.
- Replace the member-saved-principal/email fallback for delegated connections
  with a trusted mapping. Revalidate or invalidate existing credential-marker
  rows during migration; do not grandfather arbitrary impersonation targets.
- Canonicalize the configured Kerberos realm safely. Do not blindly uppercase
  the whole UPN or assume every email suffix is a Kerberos realm. Test supported
  alternate UPN suffixes and reject unsupported mappings explicitly.
- Keep normal users unable to change another user's UPN, SID, provider binding,
  delegation marker, or connection mode through UI or API.
- Check BOW organization, connection, and credential policy before delegation.
  A local break-glass login is not proof of a directory identity.
- Retain the engine-activation fix and per-identity pool separation. Include
  cold start, reconnect, pool eviction, credential invalidation, cancellation
  side connections, and concurrent service/delegated operation in the review.
- Define a fail-closed SQL identity verification strategy before user SQL runs:
  compare the authenticated original login's stable SID with the trusted AD
  identity using a validated binary representation. Names and UPN display formats
  alone are insufficient. On mismatch, invalidate the connection and return no
  user data; do not fall back to the service account or another authentication mode.
- Document that SQL's permissions remain authoritative. BOW admin status or
  connection ownership must not silently give members service-account privileges.

### E. Deployment and operations

- Provide validated configuration and a migration guide for current insecure
  settings/fallback behavior. Changes that affect availability need release notes.
- Keep the LDAP lookup password and the Kerberos service keytab separate. Prefer
  separate least-privilege accounts and independent rotation schedules.
- Mount keytabs read-only; keep user ticket caches private, per worker and per
  identity. Never put credentials into images, source, connection screenshots,
  command arguments/logs, LLM prompts, or result artifacts.
- Configure private DNS, clock synchronization, KDC reachability, scoped SQL
  SPNs, constrained delegation, certificate trust, and restricted network paths.
- Validate SQL TLS certificates as well as LDAP/BOW HTTPS certificates. Prior
  SQL lab use of `TrustServerCertificate=yes` is not production acceptance.
- Update supported deployment templates, including Helm if they need new secret,
  CA, or configuration mounts. Plain Docker must remain a supported lab option.
- Emit sanitized metrics for login outcomes, directory latency, sync completeness,
  last successful sync, revocations, ticket refreshes, and identity mismatches.
  Audit privileged configuration changes without storing raw passwords/tickets.
- Document rollback, recovery, secret rotation, minimum permissions, licensing,
  monitoring ownership, and escalation. Rollback must not re-enable insecure
  fallback or misidentify a partially validated directory snapshot as complete.

## 5. Proposed code and test map

| Area | Starting points to inspect/change |
| --- | --- |
| LDAP configuration and validation | `backend/app/settings/bow_config.py::LDAPConfig`, `configs/bow-config.dev.yaml` |
| TLS, lookup, bind, paging | `backend/app/ee/ldap/connection.py` |
| Login policy and provisioning | `backend/app/core/auth.py`, user/provider identity models and migrations as needed |
| Group ownership and scope | `backend/app/ee/ldap/sync_service.py`, `jobs.py`, membership/role models |
| Admin surfaces and errors | `backend/app/ee/ldap/routes.py`, `schemas.py`, `frontend/pages/settings/identity-provider.vue` |
| Delegated identity and connection admission | `backend/app/services/connection_identity.py`, `connection_service.py` |
| Tickets, connection pools, identity checks | `backend/app/data_sources/kerberos.py`, `clients/mssql_client.py`, `engine_pool.py`, `query_cancellation.py` |
| Existing regression starting points | `backend/tests/e2e/test_ldap.py`, `test_sso_only_local_admin_fallback.py`, `test_kerberos_sso_member_overlay.py`, `backend/tests/unit/test_mssql_kerberos.py` |

Follow backend/test guides and relevant skills during future implementation.
UI/string changes also require localization and UI-evidence procedures. New
identity persistence requires migration and compatibility tests. This list is a
navigation map, not authorization to change every listed file.

## 6. Lab preparation plan

Reuse the existing isolated AWS AD/SQL host and Linux BOW worker. Keep exact
resource identifiers and addresses in a private operator inventory, not this
document. For a representative production-topology gate, use a separate member
server for SQL rather than relying solely on the convenient all-in-one lab.
SharePoint is not required for the LDAP-to-SQL test.

Required setup, to perform only when execution is approved:

1. Record the BOW revision/image digest and AD, SQL, OS, ODBC, GSSAPI, and LDAP
   client versions. Take a recoverable snapshot of lab configuration.
2. Issue a valid directory certificate with the correct FQDN and install its CA
   trust in BOW. Enable verified HTTPS for BOW and validated SQL TLS.
3. Create a scoped read-only lookup account, BOW admission group, and synthetic
   users. Do not use a domain administrator for routine LDAP operations.
4. Ensure the delegation account, private keytab, SQL SPN, and restricted KCD
   allowlist are correct. No unconstrained delegation or automatic trust expansion.
5. Configure BOW's deployment-level LDAP settings, explicitly selected organization
   scope, login policy, role mapping, session policy, and trusted principal mapping.
6. Configure **one shared SQL connection** with delegated user authentication.
   Users must not enter SQL passwords, upload personal keytabs, or manually select
   principals to make the acceptance flow work.
7. Prepare a separate explicit service-account connection for comparison; do not
   make it a silent fallback for the delegated connection.
8. Use an authorized test LLM for the prompt gate. Supply its credential through
   the approved secret mechanism. Without it, mark prompt E2E blocked—not passed
   based on direct connector tests or a simulated model response.
9. Keep the lab private. A local tunnel/browser setup that changes the Mac requires
   the user's separate approval; browser automation may instead run on the lab
   worker. Do not expose AD, SQL, or administrative interfaces publicly for ease.

### Synthetic identities and data

| Actor | BOW access | SQL expectation |
| --- | --- | --- |
| Analyst | LDAP-admitted ordinary member | Sales and restricted finance allowed |
| Reader | LDAP-admitted ordinary member | Sales allowed; finance explicitly denied |
| Directory outsider | Valid AD account, outside admission scope | No BOW organization/connection access |
| Disabled/revoked member | Previously admitted user | New login denied; existing access revoked within approved bound |
| Local break-glass admin | Separate local recovery identity | No implicit LDAP identity or member impersonation |
| Delegation service | Runtime-only service identity | Limited documented rights; must never appear as a delegated user's SQL identity |

Seed an exact known sales aggregate and a unique synthetic marker in the finance
table. The marker must appear only in authorized outputs. Use different emails,
UPNs, and display names in selected cases to detect assumptions that they are
interchangeable. Keep test passwords distinct; never reuse administrator secrets.

## 7. Verification stages and mandatory gates

### Gate A — deterministic regressions before fixes

Add failing tests for each security contract before implementation, mocking only
external boundaries. Keep real login, authorization, mapping, and reconciliation
logic in scope. Run API/model behavior on SQLite and PostgreSQL.

Required cases include certificate rejection; TLS-before-bind on both bind paths;
empty password; filter metacharacters and ambiguous identity; typed failures;
no fallback; directory login in `sso_only`; stable identity/account-linking rules;
complete paging and partial-snapshot rejection; membership ownership; organization
isolation; and rejection of user-controlled delegation targets.

Tests must demonstrate failure against the original behavior and passing after
the relevant fix. Existing heavily mocked endpoint tests are not substitutes.

### Gate B — real directory protocol and synchronization

| Scenario | Required result |
| --- | --- |
| Valid LDAPS / supported StartTLS | Both lookup and user bind succeed only over verified TLS. |
| Wrong hostname, untrusted CA, expired cert, failed TLS upgrade | Authentication fails closed; no bind credential sent before secure transport. |
| Wrong/empty password, disabled/locked account, duplicate identity | No BOW session; sanitized non-enumerating user error. |
| Directory timeout, TLS error, configuration error | No normal-user local-password fallback. Approved break-glass policy remains distinct. |
| More users/groups than one page | Exact complete snapshot, including later-page users and groups. |
| Large AD group / nested group | Complete supported membership semantics, or explicit unsupported-case rejection. |
| Timeout/error after the first page | No membership/group deletion; last complete state retained and staleness reported. |
| Valid empty directory versus incomplete result | Only confirmed complete emptiness may trigger configured reconciliation. |
| Repeat sync / overlapping manual and background sync | Idempotent grants; no duplicate roles, partial updates, or stale-snapshot removals. |
| Manual/invited member, other-provider grant, second organization | Unrelated access preserved; no cross-organization enrollment. |
| User/group removal and account rename/recreation | Correct stable-identity behavior and revocation within the approved bound. |

Transport evidence must not capture reusable passwords or tokens. Instrument
operation ordering and TLS state; do not retain plaintext credential packet captures.

### Gate C — full browser login → BOW → SQL, without an LLM

Use separate browser contexts with no shared cookies, plus an operator-controlled
read-only SQL observation path. Do not inject principals or bypass login APIs.

1. Analyst logs in through BOW's normal LDAP-enabled login form. Verify a valid
   BOW session, provider binding, organization admission, and non-admin role.
2. Reader repeats in a separate browser session. Verify a distinct stable identity.
3. Each opens the same delegated SQL connection and verifies connection/catalog
   access through the real UI and API. A manual principal override is not allowed.
4. Execute sales queries through BOW; compare exact seeded results.
5. Analyst reads finance successfully. Reader's direct API attempt is denied even
   if the UI hides the table. No service-identity retry is allowed.
6. Correlate each request to trusted directory identity and SQL authentication:

   ```sql
   SELECT ORIGINAL_LOGIN() AS original_login,
          SUSER_SNAME() AS current_login,
          SUSER_SID(ORIGINAL_LOGIN()) AS original_login_sid,
          CONVERT(varchar(30), CONNECTIONPROPERTY('auth_scheme')) AS auth_scheme;
   ```

   Match the SID to the expected directory object using validated AD/SQL binary
   normalization. Display-name equality is insufficient. Do not grant broad SQL
   monitoring privileges to application users just to run evidence queries.

7. Attempt to change another user's identity through credentials, connection,
   profile, marker, organization, and direct API payloads. Deny before delegation.
8. Disable/remove a user, synchronize as appropriate, then retry with an existing
   session and a new login. Measure and verify the approved revocation deadline.

### Gate D — prompt-based E2E, results, and secondary data paths

- In each LDAP-authenticated browser, issue a sales-total prompt. Verify actual
  generated/executed SQL, selected connection, runtime SQL identity, seeded
  result, and final BOW response/chart—not only assistant narration.
- Request restricted finance data as each user. The analyst succeeds; the reader
  receives an appropriate refusal/permission failure with no finance marker in
  tables, charts, downloads, logs, model context, or cached responses.
- Attempt prompt instructions to use an administrator/service identity. The
  model must not control authentication policy or select an impersonation target.
- Exercise catalog refresh, schema/sample-data retrieval, exports, saved-report
  reruns, and relevant background jobs. Define ownership/permission semantics
  before testing; do not let a privileged discovery or result cache leak data to
  a less-privileged user. Authorized explicit report sharing is a separate policy.
- Capture sanitized screenshots/video for both users and a redacted execution
  trace proving which identity actually reached SQL.

### Gate E — concurrency, reconnects, and failure recovery

- Repeat cold-start tests in at least ten fresh BOW processes/containers, with
  eight concurrent sessions and at least 100 identity-checked queries per run.
  Alternate analyst, reader, and separately configured service-account traffic.
  **Any wrong identity or restricted-data leak is an immediate failure.**
- Cover warm pools, eviction, lost SQL connections, reconnects, credential-cache
  invalidation, ticket renewal/expiry, and independently restarted workers.
- Demonstrate established long-running queries overlap; do not pass by globally
  serializing complete user queries. Use barriers for deterministic tests and
  record timing only as supplementary live evidence.
- Test query timeout/cancellation and any secondary connections with correct
  identity and ownership; cancellation must not affect another user's query.
- Rotate lookup credentials, keytabs, and CA certificates with their documented
  rollout procedures. Verify intended recovery and rejection of stale credentials
  after the defined overlap period; never fall back to another identity.
- Test DNS/KDC/directory outages, clock skew, duplicate/missing SPNs, and users
  ineligible for delegation. Fail explicitly; do not weaken AD protections.
- Deliberately simulate a wrong SQL identity at a driver boundary to prove the
  fail-closed check blocks user SQL and discards the connection.

## 8. Evidence, rollout, and definition of done

For every gate, record revision/digest, versions, test IDs, sanitized setup,
expected/actual results, pass/fail/blocked status, and bounded conclusions.
Preserve runnable scripts and regression tests; redact credentials, cookies,
tickets, keytabs, real usernames/emails/SIDs, customer content, and infrastructure
identifiers from repository/PR artifacts. Keep necessary detailed operator
evidence in restricted lab storage with retention limits.

Recommended delivery order:

1. Approve policy decisions and support boundaries.
2. Land transport/authentication regressions and fixes.
3. Land scoped, ownership-aware synchronization and identity migration.
4. Land trusted delegation mapping and identity mismatch protection.
5. Verify deterministic suites, real-directory gates, and browser/prompt gates.
6. Review security-sensitive changes, deployment templates, migration behavior,
   rollback, and sanitized evidence; run a limited pilot before general rollout.

Release acceptance checklist:

- [ ] All L1–L8 findings resolved or explicitly removed from the supported scope.
- [ ] No unverified TLS, cleartext bind, ordinary-user fallback, or user-controlled impersonation.
- [ ] Directory snapshots are complete before removals; unrelated memberships are preserved.
- [ ] Stable identity mapping, tenant boundaries, migration, and revocation meet approved policy.
- [ ] SQL identities and permission outcomes verified through real LDAP-authenticated BOW sessions.
- [ ] Actual prompt E2E and secondary data paths pass; no restricted-data leakage.
- [ ] Cold/warm/reconnect/rotation tests pass with zero identity mismatches.
- [ ] SQLite/PostgreSQL regression suites and supported AD/SQL/deployment matrix pass.
- [ ] Deployment docs, operational ownership, break-glass, rollback, and secret rotation reviewed.
- [ ] No unresolved release-blocking security findings; security review approves the evidence.

Until these gates pass, describe the feature as under hardening/verification,
not as production-certified. A partial lab success must list the remaining gates.

## 9. Inputs needed when execution is requested

The existing lab supplies AD, SQL, worker capacity, and synthetic-data scaffolding.
Confirm the policy decisions above, permitted lab changes/cost limits, supported
customer topology, and an authorized LLM credential for prompt E2E. Supply secrets
only through the approved secret store. No customer credentials or real customer
data are needed. Ask separately before local Mac changes beyond the authorized
repository/worktree scope.

Implementation, lab setup, and test execution remain **not started by this plan**.
