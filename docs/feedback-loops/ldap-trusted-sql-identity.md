# Feedback loop — LDAP login must not authorize arbitrary SQL impersonation

Status: implementation and verification in progress. **Not production-certified.**

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

## Remaining gates

Live directory transport/negative cases; PostgreSQL; supported group semantics;
safe account-linking/migration operations; scope changes and sync preview parity;
all credential API paths; TLS configuration and packaging; throttling;
actual browser/LLM SQL identity and permission checks; secondary data paths;
fresh-process concurrency/rotation; screenshots; deployment and security review.

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
Deployment and after-browser verification of this fix are pending.

**Limits:** LLM and full browser query flows remain unverified.
One fresh container and twenty concurrent requests are not the full
rotation/multi-worker matrix. No browser screenshots or production-readiness
claim yet. Do not treat this checkpoint as release acceptance.
