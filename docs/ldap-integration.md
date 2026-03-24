# LDAP / Active Directory Integration

Bag of Words supports LDAP and Active Directory for two purposes:

1. **Authentication** — Users sign in with their LDAP credentials instead of a local password
2. **Group Sync** — LDAP groups are automatically synced into the application, managing org membership and access control

Authentication is available on all plans. Group sync requires an Enterprise license.

---

## Quick Start

Add the following to your `bow-config.yaml`:

```yaml
ldap:
  enabled: true
  url: ldaps://ad.company.com:636
  bind_dn: cn=svc-bow,ou=ServiceAccounts,dc=company,dc=com
  bind_password: ${BOW_LDAP_BIND_PASSWORD}
  base_dn: dc=company,dc=com
```

Set the bind password as an environment variable:

```bash
export BOW_LDAP_BIND_PASSWORD=your-service-account-password
```

Restart the application. Users can now sign in with their LDAP email and password.

---

## Configuration Reference

All LDAP settings live in the `ldap:` section of `bow-config.yaml`. These are global to the deployment (not per-organization).

### Connection

| Setting | Default | Description |
|---------|---------|-------------|
| `enabled` | `false` | Enable LDAP integration |
| `url` | — | LDAP server URL. Use `ldaps://` for SSL or `ldap://` for plain/StartTLS |
| `bind_dn` | — | Distinguished name of the service account used for searches |
| `bind_password` | — | Password for the service account. Supports `${ENV_VAR}` syntax |
| `use_ssl` | `true` | Use SSL (LDAPS). Set to `false` if using StartTLS or plain LDAP |
| `start_tls` | `false` | Use StartTLS on a plain LDAP connection |
| `connection_timeout` | `10` | Connection timeout in seconds |

### User Search (Authentication)

| Setting | Default | Description |
|---------|---------|-------------|
| `base_dn` | — | Root search base for all LDAP queries |
| `user_search_base` | (uses `base_dn`) | Base DN for user searches. Example: `ou=Users,dc=company,dc=com` |
| `user_search_filter` | `(objectClass=person)` | LDAP filter to identify user entries |
| `user_email_attribute` | `mail` | Attribute containing the user's email address |
| `user_name_attribute` | `displayName` | Attribute containing the user's display name |

### Group Sync (Enterprise)

| Setting | Default | Description |
|---------|---------|-------------|
| `group_search_base` | (uses `base_dn`) | Base DN for group searches. Example: `ou=Groups,dc=company,dc=com` |
| `group_search_filter` | `(objectClass=group)` | LDAP filter to identify group entries |
| `group_name_attribute` | `cn` | Attribute containing the group name |
| `group_member_attribute` | `member` | Attribute listing group members. Use `member` for Active Directory, `memberUid` for OpenLDAP |
| `group_member_format` | `dn` | How members are referenced: `dn` (full distinguished name) or `uid` (username only) |
| `sync_interval_minutes` | `60` | How often the background sync runs |

### User Provisioning

| Setting | Default | Description |
|---------|---------|-------------|
| `auto_provision_users` | `false` | Automatically create a local user account on first LDAP login |

---

## Authentication

### How It Works

When LDAP is enabled and a user submits their email and password:

1. The application searches LDAP for a user entry matching the email
2. If found, it attempts an LDAP bind with the user's DN and password
3. If the bind succeeds, a JWT session is issued (same as local auth)
4. If the bind fails, the login is rejected

The existing login page (`/users/sign-in`) works without modification. Users enter their LDAP email and password in the same form they would use for local authentication.

### Local Password Fallback

When LDAP is enabled, local password authentication is restricted:

- **Superusers** can always fall back to local password (break-glass access)
- **Regular users** must authenticate via LDAP
- **If the LDAP server is unreachable**, all users fall back to local password automatically

This ensures administrators are never locked out if the LDAP server goes down.

### Auto-Provisioning

By default, a user must already have a local account to sign in via LDAP. If you want accounts to be created automatically on first login, set:

```yaml
ldap:
  auto_provision_users: true
```

Auto-provisioned users are created with `role: member` and are marked as verified. They still need to be added to an organization (either manually or via group sync) to access any resources.

---

## Group Sync

> Requires an Enterprise license.

Group sync pulls groups from LDAP and mirrors them into the application. This automates organization membership and access control.

### How It Works

The sync runs on a configurable interval (default: every 60 minutes) and can also be triggered manually from the admin UI.

For each LDAP group found:

1. **Group creation** — If the LDAP group doesn't exist in the app, it is created with `external_provider: ldap`
2. **Membership diff** — Members are compared between LDAP and the app. Users are added or removed from the group to match LDAP
3. **Org membership** — Users added to an LDAP group are automatically given a `Membership` in the organization (role: `member`). No manual invite is needed.
4. **Org removal** — Users removed from *all* LDAP groups in an organization have their org membership deactivated (soft-deleted)

### Safety Guards

The sync is designed to avoid destructive mistakes:

- **Admin memberships are never removed.** Only `member` role memberships are affected by sync removal.
- **Users in manually-created groups are preserved.** If a user belongs to both LDAP groups and non-LDAP groups, removing them from LDAP groups does not remove their org membership.
- **Groups are soft-deleted.** LDAP groups removed from the directory are marked as deleted, not permanently erased.
- **Users not found in the app are skipped.** The sync does not auto-create user accounts (that is controlled by `auto_provision_users` on the auth side).

### Manual Sync

In the admin UI under **Settings > Identity Provider > LDAP Directory Sync**, admins can:

- **Test Connection** — Verify the LDAP server is reachable and display server info, user count, and group count
- **Preview Changes** — Dry-run showing what groups and memberships would be created, updated, or removed
- **Sync Now** — Execute an immediate sync

### Sync Status

The last sync result is displayed in the UI, showing:

- Groups created, updated, and removed
- Memberships added and removed
- Number of LDAP users not found in the app
- Any errors that occurred

---

## Example Configurations

### Active Directory

```yaml
ldap:
  enabled: true
  url: ldaps://dc01.corp.example.com:636
  bind_dn: CN=svc-bow,OU=Service Accounts,DC=corp,DC=example,DC=com
  bind_password: ${BOW_LDAP_BIND_PASSWORD}
  base_dn: DC=corp,DC=example,DC=com
  user_search_base: OU=Users,DC=corp,DC=example,DC=com
  user_search_filter: "(&(objectClass=user)(objectCategory=person))"
  user_email_attribute: mail
  user_name_attribute: displayName
  group_search_base: OU=Groups,DC=corp,DC=example,DC=com
  group_search_filter: "(objectClass=group)"
  group_name_attribute: cn
  group_member_attribute: member
  group_member_format: dn
```

### OpenLDAP

```yaml
ldap:
  enabled: true
  url: ldaps://ldap.example.com:636
  bind_dn: cn=readonly,dc=example,dc=com
  bind_password: ${BOW_LDAP_BIND_PASSWORD}
  base_dn: dc=example,dc=com
  user_search_base: ou=People,dc=example,dc=com
  user_search_filter: "(objectClass=inetOrgPerson)"
  user_email_attribute: mail
  user_name_attribute: cn
  group_search_base: ou=Groups,dc=example,dc=com
  group_search_filter: "(objectClass=posixGroup)"
  group_name_attribute: cn
  group_member_attribute: memberUid
  group_member_format: uid
```

### LDAP Auth Only (No Group Sync)

```yaml
ldap:
  enabled: true
  url: ldaps://ad.company.com:636
  bind_dn: cn=svc-bow,ou=Services,dc=company,dc=com
  bind_password: ${BOW_LDAP_BIND_PASSWORD}
  base_dn: dc=company,dc=com
```

Group sync settings can be omitted entirely. Without an Enterprise license, the group sync endpoints return `402` and the background job does not start.

---

## Troubleshooting

### Users can't log in

1. Verify the LDAP server is reachable: use **Test Connection** in the admin UI or check application logs
2. Confirm `user_email_attribute` matches what your directory uses (commonly `mail` for AD, `mail` or `uid` for OpenLDAP)
3. Check that `user_search_base` includes the OU where users reside
4. Ensure the bind account (`bind_dn`) has read access to user entries

### Group sync finds 0 users

The sync matches LDAP members to app users by email. If the email in LDAP doesn't match the email in the app, the user is skipped (counted as `users_not_found`). Check:

1. The `user_email_attribute` is correct
2. Users have been created in the app (manually, via SCIM, or with `auto_provision_users: true`)
3. Email case matches (the sync is case-insensitive)

### LDAP groups appear but memberships are empty

This usually means `group_member_attribute` or `group_member_format` is wrong:

- Active Directory uses `member` with full DNs (`group_member_format: dn`)
- OpenLDAP uses `memberUid` with plain usernames (`group_member_format: uid`)

### Superuser can log in but regular users can't

This is expected when the LDAP server is unreachable. Only superusers get local password fallback. Check LDAP server connectivity.

---

## Architecture Notes

- LDAP connection settings are **global** (per deployment), configured in `bow-config.yaml`
- Group sync and SCIM are **per-organization** — the same LDAP groups can sync into multiple orgs
- LDAP auth and group sync are independent. You can use LDAP auth without group sync, or combine LDAP auth with SCIM for user provisioning
- LDAP-synced groups are tagged with `external_provider: ldap` and are distinguishable from manually-created groups in the UI
- The `ldap3` Python library is required. Install with `pip install ldap3`
