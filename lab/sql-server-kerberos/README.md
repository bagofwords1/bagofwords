# SQL Server Kerberos SSO — self-contained lab

A one-command lab that stands up a real Kerberos domain and proves the
bagofwords SQL Server connector's per-user SSO via **Kerberos Constrained
Delegation (S4U2Self + S4U2Proxy)** — the mechanism a customer's IT asked us
to support — against **SQL Server 2022** on Linux.

Everything runs in containers; no external Active Directory or cloud is needed.
The launcher creates random lab-only passwords in a gitignored `.lab.env` file.

```
cd lab/sql-server-kerberos
./up.sh          # build, start DC + SQL 2022, run the test suite
./down.sh        # stop containers; preserve the lab domain, keytabs, and SQL data
./down.sh --reset # also remove the generated AD domain and keytab volumes
```

## What it stands up

| Container | Role |
|---|---|
| `dc` | Samba **Active Directory Domain Controller** — the KDC/LDAP/DNS. Realm `BOWLAB.LOCAL`. |
| `sql2022` | SQL Server 2022 on Linux, joined to the domain (keytab + `mssql-conf`). |
| `runner` | krb5 + python-gssapi + the **production** `app/data_sources/kerberos.py` and `MSSQLClient`, mounted read-only and exercised by `test_delegation.py`. |

The DC provisions (once) the accounts, SPNs, constrained-delegation config, DNS,
and exports keytabs into a shared volume that the SQL server and runner consume.
LDAP is used for the AD directory and SQL Server's user-to-SID lookup; the
database authentication and delegation being tested are Kerberos, not LDAP bind.
SQL startup is gated on both live DC DNS and KDC port readiness, and its domain
join retries transient startup failures.

### Directory accounts created

- `svc-bow` — the **app service account** / S4U impersonator. Has its own SPN
  (`bow/svc-bow.bowlab.local`, required for S4U2Self) and is configured for
  constrained delegation with protocol transition
  (`UF_TRUSTED_TO_AUTHENTICATE_FOR_DELEGATION` + `msDS-AllowedToDelegateTo`
  containing both `MSSQLSvc/...` SPNs).
- `mssql2022` — SQL service account holding the `MSSQLSvc/<fqdn>`
  and `MSSQLSvc/<fqdn>:1433` SPNs.
- `alice` — test user; has a SQL login + `db_datareader` on the lab DB.
- `bob` — test user with a login but **no** read grant (proves per-user identity
  actually reaches SQL Server).

## What it proves

**Tier A — the delegation core** (needs only the DC; validates
`app/data_sources/kerberos.py`):
- `KerberosTicketManager.delegated_ccache("alice@BOWLAB.LOCAL")` performs
  **S4U2Self** using only the service keytab — no user password.
- Initiating a GSSAPI context to the `MSSQLSvc` SPN performs **S4U2Proxy**,
  yielding a service ticket for alice to SQL Server 2022.

**Tier B — the SQL last mile** (needs the SQL containers up):
- `MSSQLClient(use_kerberos=True, kerberos_principal="svc-bow@...")` verifies the
  service-account flow and schema discovery through production BOW code.
- `MSSQLClient(use_kerberos=True, kerberos_impersonate="alice@...")` connects;
  `SUSER_SNAME()` returns `BOWLAB\alice` and `auth_scheme` is
  `KERBEROS`.
- The same client as `bob` is denied reading the granted table — per-user
  authorization is enforced by SQL Server, not the app.
- Concurrent service-account and delegated-Alice connects are interleaved to
  prove the process-global credential-cache switch cannot cross identities.

The one-command suite treats an unavailable SQL Server as a failure, ensuring a
green result always covers both the delegation core and the SQL last mile.

## Key requirements this lab surfaced (also in `docs/sql-server-kerberos.md`)

1. **`forwardable = true`** in `krb5.conf` is mandatory: S4U2Proxy is refused
   unless the S4U2Self evidence ticket is forwardable, which needs a forwardable
   service TGT.
2. The **middle-tier account (`svc-bow`) must have its own SPN** for the KDC to
   issue it an S4U2Self ticket.
3. Delegation must be **protocol transition** ("Use any authentication
   protocol" in AD terms); Kerberos-only fails with `KDC_ERR_BADOPTION`.

## Notes

- `dc` and the SQL containers run `--privileged`/as root only for setup (Samba
  KDC caps; SQL keytab + `mssql-conf`); the SQL engine drops to the `mssql` user.
- SQL Server Linux images are amd64-only, so `sql2022` uses Docker emulation on
  Apple Silicon. The launcher keeps the DC and runner native ARM when the local
  `bagofwords/bagofwords:latest` image is available; otherwise they also run as
  amd64. This lab is suitable for functional development testing, not
  performance or Microsoft support certification. Allow roughly 4 GB of Docker
  memory.
- The `runner` image installs `msodbcsql18` from packages.microsoft.com; in a
  network-restricted CI, pre-build/cache that image. With the cached production
  BOW image on ARM, the already-installed ODBC driver is reused instead.
- SQL data persists in gitignored `.state/mssql` so restarts are fast. To force
  a clean SQL initialization without deleting anything, stop the lab, run
  `./down.sh --reset`, and start with a new path such as
  `BOWLAB_SQL_DATA_DIR=./.state/mssql-clean ./up.sh`. The AD and keytab named
  volumes persist across normal `down.sh`/`up.sh` cycles so their domain SID and
  cryptographic keys remain aligned with the SQL logins. Neither teardown mode
  deletes bind-mounted database files.
- Manual poke at the KDC from inside `dc`:
  ```
  kinit -f -k -t /keytabs/svc-bow.keytab svc-bow@BOWLAB.LOCAL
  kvno -U alice@bowlab.local -P MSSQLSvc/sql2022.bowlab.local:1433
  ```
