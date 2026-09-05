# Feedback loop — LDAP login must not authorize arbitrary SQL impersonation

Status: scoped LDAP → Kerberos → SQL lab flow verified (2026-09-06).
**Not a blanket production certification; remaining gates are listed below.**

Baseline: umbrella branch after the Kerberos engine-activation merge.
Only synthetic identities are used in regression tests. No customer data or
credentials belong in this document or its evidence.

## Reproductions observed

- `test_ldap_transport_security.py`: nine failures before TLS/bind/filter fixes;
  all nine passed after. Five additional paging regressions failed before
  complete-snapshot handling; all fourteen then passed.
- `test_ldap_identity_security.py`: four failures showed that an unverified
  email or saved principal was accepted for delegation.
- `test_ldap_auth_security.py`: two failures showed local-password fallback
  during an outage and takeover of an existing local account by matching email.
- `test_ldap_sync_security.py`: an empty directory deleted an invited member's
  organization membership; synchronization accepted an unrelated organization.
- The combined new suite reached **23 passing tests** on SQLite, including
  successful ordinary-member LDAP-only login and subsequent session rejection
  when the directory denies access.
- The existing Kerberos/pool/cancellation/SSO-only/LDAP-admin suite reached
  **86 passing tests** after updating the former email-impersonation contract.
- SQL identity protection tests caught an additional cleanup failure for a NULL
  SID: the query was blocked but the connection was not invalidated. Fixed;
  all four identity-protection cases passed afterward.
- Latest focused transport/identity/cancellation/login/sync run: **47 passed**.
- Six ODBC-delimiter regressions failed before escaping field values and
  validating extra keyword names. After the fix, the SQL identity/ODBC and
  Kerberos unit suites passed together: **44 passed**. This checks string
  construction, not live driver interpretation of unusual credentials.

## Runnable local loop

From `backend`, using an existing Python 3.12 environment:

```sh
TESTING=true BOW_DATABASE_URL=sqlite:///db/app.db PYTHONPATH=. \
  .venv/bin/python -m pytest \
  tests/unit/test_ldap_transport_security.py \
  tests/unit/test_ldap_identity_security.py \
  tests/unit/test_mssql_directory_identity.py \
  tests/e2e/test_ldap_auth_security.py \
  tests/e2e/test_ldap_sync_security.py -q
```

Tests exercise real application policy and database behavior, with directory,
GSSAPI, and SQL-driver network boundaries substituted. They are not evidence of
live AD, SQL, browser, or LLM integration.

## Implementation areas

- `backend/app/ee/ldap/connection.py`: verified TLS before either bind, unique
  escaped lookup, complete paging, explicit AD admission and stable identity.
- `backend/app/core/auth.py`: fail-closed directory login, collision rejection,
  ordinary-member provisioning, JWT directory provenance and live revalidation.
- `backend/app/ee/ldap/sync_service.py`: organization scope, membership ownership,
  verified object mapping, serialization and rollback on reconciliation failure.
- `backend/app/services/connection_identity.py`: no email/form-based delegation.
- `backend/app/services/connection_service.py`: revalidation and expected SID.
- `backend/app/data_sources/clients/mssql_client.py`: reject wrong SQL SID/auth
  scheme before yielding a connection; preserve engine activation lock scope.
- `ldapsecurity01`: new nullable identity/ownership fields; existing accounts
  are deliberately not automatically linked.

## Remaining production gates

Supported group semantics (nested/ranged membership matrix); safe existing-account
linking/migration operations; scope changes and sync-preview parity; all credential
API paths; deployment-specific TLS packaging; throttling; rotation/failure recovery;
and deployment/security review. SharePoint delegated identity is **not** proven by
the SQL tests. These are not waived by a successful disposable lab.

## Live lab checkpoint

The live LDAPS handshake initially failed with no peer certificate. The domain
controller reported event 1220 and an untrusted certificate chain. Certificate
trust/loading was corrected in the isolated lab. A listening port and an OpenSSL
"verify return code: 0" without a completed handshake are **not** a pass.

Observed afterward, using the current LDAP manager and SQL/Kerberos source
mounted read-only into a fresh instance of the requested existing Docker image:

- Linux-to-AD TLS 1.3 handshake completed with hostname and certificate validation.
- Two synthetic users completed real LDAPS password binds with distinct GUIDs
  and SIDs; empty and incorrect passwords were rejected.
- Each authenticated directory identity delegated to SQL, passed the client SID
  guard, reported the expected original login and KERBEROS, and read sales.
- Twenty alternating requests across eight threads retained the expected user.
- The analyst read the restricted table; the reader received SQL permission
  error 229. This was an expected denial, not a test failure.

Secrets were obtained from encrypted lab parameters, passed through environment
variables, and not included in this document. The container was removed after
the check. The LDAPS public certificate is lab-scoped.

Subsequent strict-TLS run: the default certificate bypass was removed after two
failing regressions. SQL/Kerberos unit suites then passed together (46 tests).
The live client rejected the untrusted SQL certificate; after adding the lab
certificate to the disposable container trust store, both identities, twenty
concurrent requests, and the restricted-table denial passed again.

Full-app checkpoint: real HTTP admin/org bootstrap passed, followed by real
LDAP login and authenticated organization/session checks for both ordinary
members. Generic connection/data-source API regression suites: 11 passed.
The browser exposed a separate sign-in defect: in `sso_only` mode it displayed
only the heading, with no LDAP password form. Before screenshot captured in
the AWS lab. Public settings now exposes only `ldap.enabled`, and the frontend
uses it to show the form without enabling arbitrary local-password fallback.
The deployed frontend was built from checkpoint `32dfd8398`; subsequent backend
fixes below were mounted into the same isolated container. Fresh real LDAP
browser logins now display the password form and enter the app for both members.

## Final end-to-end pass and additional fixes

Two synthetic users signed in through the actual browser form. A real
`gpt-5.4-mini` prompt invoked `create_data`, generated Python using the SQL
client, and executed only this identity/constant query (no table contents):

```sql
SELECT ORIGINAL_LOGIN() AS sql_login,
       CONVERT(varchar(30), CONNECTIONPROPERTY('auth_scheme')) AS auth_scheme,
       42 AS synthetic_check;
```

Both stored results and visible browser tables showed the respective synthetic
AD login, `KERBEROS`, and `42`. The reader could not retrieve the analyst's shared
snapshot: data was withheld and executable code redacted. Disabling the reader
in real AD invalidated its existing JWT (401), while the analyst stayed valid
(200); the reader was restored afterward.

Three defects found during this pass were corrected:

1. **Cold prompt catalog:** `SchemaContextBuilder` read an absent per-user overlay
   without discovering it. Kerberos has no OAuth callback to initialize that
   cache. It now discovers under the verified caller on first use, never from
   the service catalog. After deleting only the reader's derived overlay, a real
   browser/LLM prompt still produced the reader's correct SQL identity.
2. **Empty refresh revocation:** `get_user_data_source_schema` returned early for
   `[]`, leaving stale access. A successful empty snapshot now reconciles the
   overlay; `None` is rejected as a missing snapshot. Both new overlay regressions
   failed before the fixes; all six overlay tests passed afterward.
3. **Concurrent first viewer result:** two BOW HTTP requests selected an empty
   cache slot and both inserted it, producing a unique-constraint 500. Atomic
   SQLite/PostgreSQL upsert now retains the exact step/user/parameter key in both
   query and step execution paths. A synchronized two-request endpoint regression
   passes. The live cold-cache retry passed **12 requests across six threads**,
   six per identity, with zero identity mismatches or HTTP failures.

The stale error instructing users to enter a different principal was also
replaced with the existing localized access-denied contract. Directory identity
must be verified; editable email/credential fields cannot authorize delegation.

### Regression evidence

- LDAP/Kerberos/cancellation/overlay suite on PostgreSQL: **97 passed**.
- Additional viewer-cache, report-parameter and shared-artifact suite on
  PostgreSQL: **30 passed**. Its first run failed because the disposable test
  container's uploads directory was unwritable; a scoped writable tmpfs corrected
  the harness before rerunning.
- Local combined suite initially: **105 passed, 4 failed** because four unit
  assertions still expected HTTPException after the typed AppError migration.
  Updating those assertions preserved the 403/error-code contract; the affected
  Kerberos and concurrent-query files then passed **36 tests**.
- Final combined local rerun after those updates: **109 passed** (2730 warnings).

For the added cache tests, run the existing local command with:

```sh
tests/e2e/test_kerberos_sso_member_overlay.py
tests/e2e/test_query_viewer_run_with_data_source.py
tests/e2e/test_report_rerun_params.py
tests/e2e/rbac/test_viewer_run_shared_artifacts.py
```

### Browser evidence

Evidence contains only synthetic lab users and constant query output:

- `media/pr/ldap-trusted-sql-identity/ldap-signin-before.png`
- `media/pr/ldap-trusted-sql-identity/ldap-signin-after.png`
- `media/pr/ldap-trusted-sql-identity/ldap-analyst-final-table.png`
- `media/pr/ldap-trusted-sql-identity/ldap-reader-final-table.png`
- `media/pr/ldap-trusted-sql-identity/ldap-browser-verification.gif`

The before image predates the separately merged sign-in redesign, so it proves
the missing form, not a pixel-identical base. Reusing saved browser sessions
stalled one capture; fresh real LDAP logins produced both final table captures.
The successful prompt explicitly selected `create_data`: general-purpose tool
selection is not claimed perfect. Restricted-table denial was verified at the
SQL client layer in the earlier checkpoint, not through a final browser prompt.

Temporary upstream LLM credentials were cleared from the lab provider and its
encrypted SSM parameter; temporary browser/admin session files and eight named
disposable containers were removed. Stored synthetic query evidence remains.
The two lab EC2 instances are stopped (not terminated); persistent disks remain
recoverable and may continue to incur storage charges. Rotate any upstream key
that was pasted into chat; deleting the lab copy is not key revocation.
