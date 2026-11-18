# Multiple Organizations on One Deployment

One self-hosted deployment can host several organizations, for example one per
department. Each organization is a separate workspace with its own connections,
agents, instructions, dashboards, members, roles and admins. Everyone signs in
through the same identity provider.

Organizations are meant for teams inside one company. If two parties need legal
or regulatory separation, their own encryption keys, or their own database, run
separate deployments.

Creating an additional organization requires an Enterprise license. A
single-organization deployment needs none of the settings on this page and
behaves as it always has.

---

## Quick Start

1. Turn the feature on in `bow-config.yaml`:

   ```yaml
   features:
     allow_multiple_organizations: true
   ```

2. Name the instance administrators. They are the only people who can create
   organizations:

   ```bash
   BOW_SUPERUSER_EMAILS=it-admin@company.com,platform@company.com
   ```

3. Restart. An instance administrator opens the menu at the top left and
   chooses **Create organization**, enters a name and, optionally, the email of
   the new organization's first admin. That person receives an invite.

---

## Roles

| Role | Scope | Can do |
|---|---|---|
| Instance administrator | The whole deployment | Create organizations, manage the license, read system logs, set embedding rules, assign sign-up domains |
| Organization admin | One organization | Manage that organization's members, roles, connections, agents and settings |

`BOW_SUPERUSER_EMAILS` is a comma-separated list of email addresses. You can
also set `auth.superuser_emails` in `bow-config.yaml`; the environment variable
replaces it when both are set.

- When the list is set it is authoritative. People on it become instance
  administrators at startup and at their next sign-in. People removed from it
  lose the role.
- When the list is empty, existing instance administrators are left as they are.
- List addresses of accounts that already exist and that you know the owner of.
  The role goes to whoever holds the account with that address. An address on
  the list cannot be invited by an organization admin or registered through
  open sign-up, only through an invite from an instance administrator or a
  trusted sign-in provider.
- An organization's admins and its SCIM connection cannot add, change or
  deactivate an instance administrator's account.
- An instance administrator is not automatically a member of every organization.
  They see the organizations they belong to.

With `auth.mode: sso_only` on a multi-organization deployment, the emergency
password sign-in is limited to instance administrators.

---

## Placing People in Organizations

Signing in proves who someone is. It does not decide which organization they
land in. There are three ways to place people.

### Invites

An organization admin invites an email address from **Settings → Members**.

- An invite link joins the person to that organization only.
- Someone who already belongs to another organization is not added silently.
  They get an invite and join when they open the link while signed in.

### Identity-provider groups (recommended with Entra ID)

Map groups from your identity provider to organizations. The mapping lives in
`bow-config.yaml`, so the operator controls it, not organization admins.

```yaml
oidc_providers:
  - name: entra
    enabled: true
    issuer: https://login.microsoftonline.com/<tenant-id>/v2.0
    client_id: ${BOW_ENTRA_CLIENT_ID}
    client_secret: ${BOW_ENTRA_CLIENT_SECRET}
    group_claim: groups
    resolve_group_names: true     # only needed to match groups by display name
    org_mapping:
      - group: "5d1f8a52-0c11-4d6f-9a3e-6f0f6f1a0001"   # group object id (preferred)
        organization: "Finance"                          # organization id, or exact name
        role: member                                     # member (default) or admin
      - group: "Finance Leads"
        organization: "Finance"
        role: admin
      # compact form: "<group>:<organization>:<role>"
      - "5d1f8a52-0c11-4d6f-9a3e-6f0f6f1a0002:HR:member"
```

At each sign-in:

- A person in a mapped group becomes a member of that organization with that
  role. They do not need an invite.
- A role is only ever raised by the mapping, never lowered.
- A person who has left every group mapped to an organization is removed from
  it, if the mapping is what added them. Memberships created by an invite, by
  SCIM or by an admin are never removed by the mapping.
- An organization at its license seat limit is skipped. Sign-in still succeeds.

The identity provider must send a groups claim. For Entra ID, add the `groups`
claim to the ID token in the app registration.

Map groups by their object id. Display names are not unique in most identity
providers, and people can often create their own groups, so a mapping by name
can admit someone who creates a group with a mapped name.

### SCIM

SCIM tokens belong to one organization. Provisioning from one organization only
affects that organization's membership when the person also belongs to another.

### Sign-up domains

Automatic sign-up by email domain cannot tell departments apart when they share
one domain. On a multi-organization deployment only an instance administrator
can assign a domain to an organization, and a domain can belong to one
organization at a time. Prefer the group mapping.

---

## What Is Shared

| Each organization has its own | All organizations share |
|---|---|
| Connections and their credentials | The application database (every row belongs to one organization) |
| LLM providers and keys | One encryption key |
| Agents, instructions, dashboards, reports | The file storage location |
| Members, roles, groups | Sign-in providers |
| Audit log and audit streams | The license |
| Settings such as locale, email server and sign-up policy | Server settings under `security` |
| Slack, Teams and WhatsApp connections | The server that runs queries and generated code |

Some details follow from this:

- A person has one account. Their name, email and sign-in belong to the account,
  not to an organization.
- One Slack workspace, Teams bot or WhatsApp number can be connected to one
  organization. Connecting it to a second one is refused.
- A personal API key works only in the organization it was created in.
- License seat limits apply to each organization separately.
- LDAP sign-in admits people to a single organization.

---

## Recommended Settings

```yaml
features:
  allow_multiple_organizations: true
  allow_uninvited_signups: false

auth:
  mode: sso_only            # or hybrid

security:
  block_private_outbound: true
  # Limit each organization's file connections to its own folders (optional).
  organization_file_roots:
    "<finance organization id>": ["/mnt/finance"]
    "<hr organization id>": ["/mnt/hr"]
```

- Use PostgreSQL for the application database.
- Use the published Docker image or Helm chart, which run generated code in a
  restricted sandbox.
- With multiple organizations enabled, a first sign-in is linked to an existing
  account by email only when the provider is trusted: it is the only sign-in
  provider, it does not mark the address as unverified, and it is not a
  multi-tenant Microsoft endpoint. Otherwise the sign-in is refused, and an
  invited person must use their invite link. Set `trust_email_for_linking:
  true` on a provider whose email addresses you trust, or `false` to never link
  by email. A single-organization deployment links by email as it always has
  unless you set this to `false`.

---

## Settings Reference

| Setting | Default | Purpose |
|---|---|---|
| `features.allow_multiple_organizations` | `false` | Allow more than one organization |
| `BOW_SUPERUSER_EMAILS` / `auth.superuser_emails` | empty | Instance administrators |
| `oidc_providers[].org_mapping` | empty | Map identity-provider groups to organizations |
| `oidc_providers[].trust_email_for_linking`, `google_oauth.trust_email_for_linking` | automatic | Link a first sign-in to an existing account by email |
| `security.organization_file_roots` | empty | Folders reserved for an organization's file connections; other organizations cannot use them |
| `security.file_connector_allowed_roots` | unset | Folders any file connection may use |
| `security.slack_require_signed_events` | `false` | Refuse unsigned Slack events over HTTP |
| `security.org_scope_guard` | `off` | Log (`warn`) or refuse (`enforce`) database queries that are not limited to one organization; a diagnostic for developers |
