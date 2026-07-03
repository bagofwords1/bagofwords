# SQL Server: Kerberos authentication & per-user SSO (constrained delegation)

Bag of Words supports two Kerberos auth shapes for on-prem Microsoft SQL Server,
on top of the default SQL username/password login:

| Auth variant | Scope | Identity seen by SQL Server |
|---|---|---|
| `userpass` (default) | system or per-user | SQL login |
| `kerberos` — Kerberos (Windows Integrated) | system | the app's **service account** |
| `kerberos_delegated` — Kerberos SSO | per-user | **the signed-in user's AD identity**, via constrained delegation (S4U2Self + S4U2Proxy) |

Both Kerberos variants use the Microsoft ODBC Driver 17/18 for SQL Server on
Linux, which performs GSSAPI/Kerberos when the connection string contains
`Trusted_Connection=yes` (no NTLM fallback exists on Linux). The driver derives
the target SPN as `MSSQLSvc/<host>:<port>` and this cannot be overridden — the
connection **host must be the SQL Server's FQDN**, not an IP address or an
unregistered CNAME.

---

## Phase A — service-account Kerberos (keytab)

All queries authenticate as one AD service account. Bag of Words still enforces
its own RBAC and read-only SQL guard; SQL Server sees a single login.

### Active Directory / DBA prerequisites

1. A domain service account for the app, e.g. `svc-bow@CORP.EXAMPLE.COM`,
   with AES key types enabled ("This account supports Kerberos AES 128/256 bit
   encryption").
2. A keytab for it (on Windows):

   ```
   ktpass /princ svc-bow@CORP.EXAMPLE.COM /mapuser svc-bow /pass * ^
          /crypto AES256-SHA1 /ptype KRB5_NT_PRINCIPAL /out svc-bow.keytab
   ```

3. SQL Server's SPN registered (usually automatic):
   `setspn -L <sql service account>` should list `MSSQLSvc/<fqdn>:1433`.
4. A SQL Server login + permissions for the service account
   (`CREATE LOGIN [CORP\svc-bow] FROM WINDOWS;` + minimal read-only grants —
   the app only ever issues SELECTs, so `db_datareader` on the target database
   or explicit `GRANT SELECT` on the approved schemas is enough and recommended).

### Server / deployment setup

The Docker image ships `krb5-user`, `libgssapi-krb5-2`, and `msodbcsql18`.
Mount two files and set one env var:

```yaml
# docker-compose override / k8s equivalent
services:
  bagofwords:
    volumes:
      - ./krb5.conf:/etc/krb5.conf:ro
      - ./svc-bow.keytab:/etc/bagofwords/svc-bow.keytab:ro   # readable by the 'app' user
    environment:
      # GSSAPI initiates from the keytab automatically — no kinit cron needed.
      KRB5_CLIENT_KTNAME: /etc/bagofwords/svc-bow.keytab
```

Minimal `krb5.conf`:

```ini
[libdefaults]
    default_realm = CORP.EXAMPLE.COM
    dns_lookup_kdc = true
    forwardable = true

[domain_realm]
    .corp.example.com = CORP.EXAMPLE.COM
    corp.example.com = CORP.EXAMPLE.COM
```

Clock skew must stay under 5 minutes (run NTP/chrony on the host).

Smoke test from inside the container:

```
kinit -kt /etc/bagofwords/svc-bow.keytab svc-bow@CORP.EXAMPLE.COM && klist
```

### App configuration

Create (or edit) the SQL Server connection and pick **Kerberos (Windows
Integrated)** as the authentication method. Leave *Service Principal* blank to
use the default credential cache (`KRB5_CLIENT_KTNAME` above); set it only when
the keytab holds several principals. Verify on the SQL side with:

```sql
SELECT auth_scheme FROM sys.dm_exec_connections WHERE session_id = @@SPID;
-- expect: KERBEROS
```

---

## Phase B — per-user SSO via Kerberos Constrained Delegation

With `auth_policy = user_required`, queries run as **the signed-in user's AD
identity**. No password or token is collected from the user: the app performs
protocol transition (S4U2Self — "issue me a ticket *for user X* to myself",
which requires only the user's UPN) and constrained delegation (S4U2Proxy —
exchange it for a ticket to the SQL Server SPN). SQL-side permissions, row-level
security, and auditing all apply per user.

The user's UPN is taken from their Bag of Words login identity (Entra ID /
OIDC `preferred_username`/`upn`, LDAP, or local email). If a user's email is not
their AD UPN, they can save an explicit principal under the data source's
user credentials ("Kerberos SSO" mode).

### Additional AD prerequisites (on top of Phase A)

Delegation must be configured on the **app's service account** — this is the
piece the AD team has to approve:

1. **Classic KCD with protocol transition** (single domain): in ADUC →
   `svc-bow` → *Delegation* tab →
   *"Trust this user for delegation to specified services only"* →
   **"Use any authentication protocol"** (this is required; "Kerberos only"
   will fail with `KDC_ERR_BADOPTION`), and add the SQL Server's
   `MSSQLSvc/<fqdn>:1433` SPN(s) to the allowed list
   (`msDS-AllowedToDelegateTo`).

   **Or** resource-based constrained delegation (works cross-domain): on the
   SQL Server's service account,
   `Set-ADUser <sqlsvc> -PrincipalsAllowedToDelegateToAccount svc-bow`.

2. SQL Server logins for the end users or their AD groups:
   `CREATE LOGIN [CORP\bi-analysts] FROM WINDOWS;` + per-group grants.

3. Note: members of **Protected Users** or accounts flagged *"Account is
   sensitive and cannot be delegated"* cannot be impersonated — those users
   need a personal SQL login (`userpass` mode) instead.

Smoke test (from the container, validates the whole AD chain before touching
the app):

```
kinit -kt /etc/bagofwords/svc-bow.keytab svc-bow@CORP.EXAMPLE.COM
kvno -U jdoe@corp.example.com -P MSSQLSvc/sqldwh01.corp.example.com:1433
```

### App configuration

1. Ensure the image has the Kerberos extra (the default Dockerfile installs it:
   `uv sync --extra kerberos`, i.e. python-gssapi).
2. Configure the connection with **Kerberos (Windows Integrated)** system auth
   (Phase A) — the system identity is still used for schema indexing.
3. Enable **Require user authentication** on the connection. For a Kerberos
   system-auth connection this defaults `allowed_user_auth_modes` to
   `["kerberos_delegated"]` — per-user SSO is then automatic, with no user
   action needed.
4. Optional env vars:
   - `BOW_KRB5_CCACHE_DIR` — directory for per-user credential caches
     (default `/tmp/bow_krb5`, created `0700`).
   - `KRB5_CLIENT_KTNAME` — service keytab used both for the app's own identity
     and as the impersonator for S4U (required for Phase B).

Per-user auth on tabular sources requires an **enterprise license**.

### Operational notes & limitations

- **Ticket renewal** is handled by the app: delegated tickets are cached per
  user and re-acquired near expiry from the keytab; nothing to cron.
- `KRB5CCNAME` is process-global on Linux, so the credential cache switch is
  serialized around the driver's connect handshake. Concurrent queries by
  different users queue for milliseconds at connect time; established
  connections are unaffected.
- Do not enable unixODBC connection pooling: pooled integrated-auth connections
  can be reused across identities.
- Indexing/schema refresh (no user in context) always runs as the service
  account.
- If SQL Server is **2022+ and Azure Arc-enabled**, Entra-token authentication
  is an alternative to KCD that reuses the app's existing Entra OBO flow —
  worth considering where AD delegation approval is hard to obtain.

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Cannot generate SSPI context` / `SPNEGO` errors | SPN missing or host is an IP/alias — connect by the exact FQDN the SPN is registered for |
| `KDC_ERR_BADOPTION` on delegation | Delegation set to "Kerberos only" — must be "Use any authentication protocol", or the `MSSQLSvc` SPN is missing from `msDS-AllowedToDelegateTo` |
| `KRB5KRB_AP_ERR_SKEW` | Clock skew > 5 min — fix NTP |
| Works with `kinit` but not in app | Keytab not readable by the container's `app` user, or `KRB5_CLIENT_KTNAME` unset |
| Delegated auth fails for one user only | Protected Users / "sensitive, cannot be delegated" flag, or their email ≠ AD UPN (save an explicit principal) |
| `auth_scheme` shows `NTLM`/`SQL` | Connection fell back to another path — check the connection uses the Kerberos auth variant |
