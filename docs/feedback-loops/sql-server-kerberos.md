# SQL Server Kerberos identity-isolation feedback loop

## Root cause validated

The connector had three correctness gaps that only become visible when Kerberos
is exercised under realistic conditions:

1. A default service-account connect did not acquire the process-wide
   `KRB5CCNAME` lock. It could therefore observe a delegated user's temporary
   cache during a concurrent handshake. The affected activation path was
   `backend/app/data_sources/kerberos.py:128`.
2. Credential-cache filenames were stable across manager/process instances,
   allowing different workers to target the same file. The allocation path was
   `backend/app/data_sources/kerberos.py:172`.
3. `Encrypt` was omitted when enabled, so ODBC Driver 17 and 18 selected
   different defaults. The connection-string path was
   `backend/app/data_sources/clients/mssql_client.py:101`.

Two status paths also treated a previously successful Kerberos check as still
verified after a later check recorded an error:
`backend/app/services/connection_identity.py:347` and
`backend/app/services/connection_service.py:1057`.

## Loop A — deterministic reproduction

Run from the repository root:

```bash
cd backend
PYTHONWARNINGS=ignore .venv/bin/pytest tests/unit/test_mssql_kerberos.py -q
```

Before the fix this produced:

```text
5 failed, 24 passed
```

The failures proved that enabled encryption lacked an explicit ODBC keyword,
the default-cache path did not serialize with delegated activation, separate
ticket managers generated the same cache path, and a failed recheck remained
reported as successful.

After the fix, run the focused and adjacent regression set:

```bash
cd backend
PYTHONWARNINGS=ignore .venv/bin/pytest \
  tests/unit/test_mssql_kerberos.py \
  tests/unit/test_engine_pool_identity.py \
  tests/e2e/test_kerberos_sso_member_overlay.py -q
```

Expected result:

```text
45 passed
```

## Loop B — live confirmation

The self-contained lab uses Samba as an AD domain controller/KDC, joins SQL
Server 2022 to that domain, and mounts the production backend code read-only
into the test runner. LDAP supplies directory and SID lookup; all database
authentication in this proof is Kerberos.

```bash
cd lab/sql-server-kerberos
./up.sh -vv
```

The live suite must prove all seven checks:

- service keytab exported with restricted permissions;
- S4U2Self obtains Alice's delegated credential without her password;
- S4U2Proxy obtains the SQL Server service ticket;
- the production `MSSQLClient` connects as `BOWLAB\svc-bow` with
  `auth_scheme = KERBEROS` and discovers `dbo.sales`;
- 16 interleaved service/Alice handshakes never swap identities;
- the delegated client connects as `BOWLAB\alice`, reports Kerberos, and reads
  the three seeded rows;
- Bob reaches SQL Server as himself but is denied access to `dbo.sales`.

Expected result:

```text
7 passed
```

For a from-scratch check, point the lab at a new ignored SQL data directory:

```bash
cd lab/sql-server-kerberos
./down.sh --reset
BOWLAB_SQL_DATA_DIR=./.state/mssql-clean ./up.sh -vv
```

This validates domain provisioning, the SQL domain join and SSSD identity
lookup, keytab installation, SQL login creation, production client wiring, and
the query path rather than relying on a warm database.

## Fix

- Hold one re-entrant process lock for both default and delegated handshake
  activation, including service credential acquisition.
- Allocate credential caches under a private, mode-`0700`, per-manager worker
  namespace.
- Always emit `Encrypt=yes` or `Encrypt=no` explicitly.
- Treat `last_error` as authoritative in both member status and the admin
  verification roster.
- Register the MSSQL client path explicitly and exercise it through the live
  runner.
- Gate SQL startup on live AD DNS/KDC readiness and retry the domain join; a
  keytab-file-only health check raced the first clean startup.
- Persist the generated AD database/config and keytab volume across normal
  restarts so a DC image rebuild cannot silently replace the domain while SQL
  still contains the previous domain's logins. `down.sh --reset` is the explicit
  clean-domain path.
- Regenerate the dedicated client `krb5.conf` on both provision and reuse. The
  Samba server template omits `forwardable=true`; publishing it on restart made
  S4U2Self pass but S4U2Proxy fail with `KDC_ERR_BADOPTION`.

## Proof and regression notes

- Unit/adjacent regression suite: `45 passed`.
- Clean SQL initialization plus live delegation: `7 passed`.
- After the lifecycle fixes, two consecutive restart launches: `7 passed`
  each, including DC container recreation against the persisted domain and
  keytabs.
- SQL Server's Linux image is amd64-only; on Apple Silicon this is a functional
  emulation proof, not a performance or vendor-support certification.
- Customer deployment still requires the system team to create the service
  account/keytab, register both BOW and `MSSQLSvc` SPNs, enable constrained
  delegation with protocol transition, supply DNS/time synchronization, and
  create least-privilege SQL logins or AD-group grants.
