# SQL Server Kerberos SSO — self-contained lab

A one-command lab that stands up a real Kerberos domain and proves the
bagofwords SQL Server connector's per-user SSO via **Kerberos Constrained
Delegation (S4U2Self + S4U2Proxy)** — the mechanism a customer's IT asked us
to support — against **SQL Server 2022 and 2019** on Linux.

Everything runs in containers; no external Active Directory, cloud, or secrets
are needed.

```
cd lab/sql-server-kerberos
./up.sh          # build, start DC + SQL 2022/2019, run the test suite
./down.sh        # tear down (containers + volumes)
```

## What it stands up

| Container | Role |
|---|---|
| `dc` | Samba **Active Directory Domain Controller** — the KDC/LDAP/DNS. Realm `BOWLAB.LOCAL`. |
| `sql2022` | SQL Server 2022 on Linux, joined to the domain (keytab + `mssql-conf`). |
| `sql2019` | SQL Server 2019 on Linux, same. |
| `runner` | krb5 + python-gssapi + the **production** `app/data_sources/kerberos.py` and `MSSQLClient`, mounted read-only and exercised by `test_delegation.py`. |

The DC provisions (once) the accounts, SPNs, constrained-delegation config, DNS,
and exports keytabs into a shared volume that the SQL servers and runner consume.

### Directory accounts created

- `svc-bow` — the **app service account** / S4U impersonator. Has its own SPN
  (`bow/svc-bow.bowlab.local`, required for S4U2Self) and is configured for
  constrained delegation with protocol transition
  (`UF_TRUSTED_TO_AUTHENTICATE_FOR_DELEGATION` + `msDS-AllowedToDelegateTo`
  containing both `MSSQLSvc/...` SPNs).
- `mssql2022`, `mssql2019` — SQL service accounts holding the `MSSQLSvc/<fqdn>`
  and `MSSQLSvc/<fqdn>:1433` SPNs.
- `alice` — test user; has a SQL login + `db_datareader` on the lab DB.
- `bob` — test user with a login but **no** read grant (proves per-user identity
  actually reaches SQL Server).

## What it proves

**Tier A — the delegation core** (needs only the DC; validates
`app/data_sources/kerberos.py`):
- `KerberosTicketManager.delegated_ccache("alice@BOWLAB.LOCAL")` performs
  **S4U2Self** using only the service keytab — no user password.
- Initiating a GSSAPI context to each `MSSQLSvc` SPN performs **S4U2Proxy**,
  yielding a service ticket for alice to SQL Server 2022 and 2019.

**Tier B — the SQL last mile** (needs the SQL containers up):
- `MSSQLClient(use_kerberos=True, kerberos_impersonate="alice@...")` connects to
  each SQL version; `SUSER_SNAME()` returns `BOWLAB\alice` and `auth_scheme` is
  `KERBEROS`.
- The same client as `bob` is denied reading the granted table — per-user
  authorization is enforced by SQL Server, not the app.

Tier A runs even if the SQL containers are unavailable (Tier B auto-skips),
so the novel code is always validated.

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
- Requires ~6 GB free RAM (two SQL Server instances) and amd64 (SQL Server on
  Linux images are amd64-only).
- The `runner` image installs `msodbcsql18` from packages.microsoft.com; in a
  network-restricted CI, pre-build/cache that image.
- Manual poke at the KDC from inside `dc`:
  ```
  kinit -f -k -t /keytabs/svc-bow.keytab svc-bow@BOWLAB.LOCAL
  kvno -U alice@bowlab.local -P MSSQLSvc/sql2022.bowlab.local:1433
  ```
