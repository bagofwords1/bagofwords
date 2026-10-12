# Feedback Loop — "I want each member to bring their own identity" (Qlik on-prem, Postgres)

A Qlik Sense on-prem connection authenticates BOW with one client certificate
that can act as **any** Qlik account; the account a request runs as is just the
`X-Qlik-User` header. Before this change a member chose that account by
**typing** their own User Directory / User ID — so any member could type a
colleague's (or the CEO's) account and read their data. The ask: every member
gets their own identity, from something they cannot edit, and it should work
for Qlik and for an ordinary database.

Claim validated here: an admin sets per-member attributes (invite, edit, CSV
import); a connection maps its identity slots to those attributes; every query a
member makes runs as their mapped identity; a member with no value is blocked —
never handed the connection's own identity.

## Root cause (validated)

- The member-typed identity was a user-scoped overlay variant on the Qlik
  registry entry (`QlikSenseOnPremUserIdentityCredentials`, scope `user`,
  `overlay=True`), merged over the certificate at query time. Nothing tied the
  typed value to the signed-in member.
- The client used it verbatim: `qlik_sense_onprem_client.py` `_acting_user()`
  returns `UserDirectory=…; UserId=…` from `user_id`, which was also a public,
  reassignable attribute.
- There was no admin-owned per-member data to derive an identity from:
  `Membership.profile_attributes` is IdP-synced and replaced wholesale on each
  login (`ee/oidc/profile_service.py` `store_profile_attributes`).

## Loop A — deterministic (no external services)

```bash
cd backend
TESTING=true BOW_DATABASE_URL=sqlite:///db/test.db uv run pytest \
  tests/unit/test_connection_identity_mapping.py \
  tests/unit/test_qlik_sense_onprem_client.py -q
TESTING=true BOW_DATABASE_URL=sqlite:///db/test.db uv run pytest \
  tests/e2e/test_member_attributes_identity_mapping.py -m e2e --db=sqlite -q
```

Observed: `137 passed` (unit) and `20 passed` (e2e). With the resolver disabled
(`resolve_identity_params` returning `None`), the e2e loop fails exactly where
it should:

```
FAILED ...::test_each_member_acts_as_their_own_mapped_identity
FAILED ...::test_member_without_a_value_is_blocked_not_given_the_shared_identity
```

## Loop B — live sandbox (real Postgres 16, real UI, mock QRS)

1. Postgres with one login user and two roles (the connection logs in as
   `bow_gateway`, which may read everything; members become a role):

   ```sql
   CREATE ROLE sales_team NOLOGIN; CREATE ROLE finance_team NOLOGIN;
   CREATE ROLE bow_gateway LOGIN PASSWORD '…' NOINHERIT;
   GRANT sales_team, finance_team TO bow_gateway;
   CREATE DATABASE acme;  -- \c acme
   CREATE TABLE orders (...); CREATE TABLE payroll (...);
   GRANT SELECT ON orders  TO sales_team, finance_team, bow_gateway;
   GRANT SELECT ON payroll TO finance_team, bow_gateway;
   ```

2. Boot the stack (sandbox-feedback-loop skill), seed an admin plus `dana`,
   `avi`, `noa` (`tools/agent/seed_org.py --invite …`), add a PostgreSQL agent
   on `acme` as `bow_gateway`.
3. Settings → Members: give `dana` `pg_role=sales_team`, `avi`
   `pg_role=finance_team`; leave `noa` empty. Two `PUT …/members/{id}` → 200;
   audit log gets `member.attributes_changed` with before/after.
4. Manage connection → **Per-member identity** → on, Session role ←
   *Member attribute* `pg_role` → Save (`connection.identity_mapping_changed`
   audited). The preview shows `dana → sales_team`, `avi → finance_team`,
   `noa`/admin **Blocked**. **Test** per member:
   `Successfully connected to PostgreSQL as role sales_team` / `… finance_team`.
5. Each member, Agent → Queries → New query → Run (same `construct_clients`
   path the chat agent uses), code: `SELECT current_user`, `count(*) FROM
   orders`, `count(*) FROM payroll`:

   | member | current_user | orders | payroll |
   |---|---|---|---|
   | dana | `sales_team` | 4 | `permission denied for table payroll` |
   | avi | `finance_team` | 4 | 3 |
   | noa | 403 — *"…your member profile has no value for Session role (membership.attr:pg_role). Ask an admin…"* | | |

   `pg_stat_statements` (records the executing role) agrees and shows nothing
   ran as `bow_gateway` for a member, and nothing at all for `noa`:

   ```
    executed_as  | query
   --------------+-----------------------------------
    finance_team | SELECT count(*) AS v FROM orders
    finance_team | SELECT count(*) AS v FROM payroll
    finance_team | SELECT current_user AS v
    sales_team   | SELECT count(*) AS v FROM orders
    sales_team   | SELECT current_user AS v
   ```

6. Qlik: a local TLS mock of the Qlik Repository Service on :4242 logs every
   `X-Qlik-User` and lists apps per user. Qlik agent (certificate auth), mapping
   User Directory ← *Fixed value* `CORP`, User ID ← *Member attribute*
   `qlik_user_id`. Member path (`construct_clients`) and the Test button send
   `UserDirectory=CORP; UserId=dana` (1 app) / `…UserId=avi` (2 apps); `noa`
   makes **zero** requests. Indexing (no signed-in user) keeps
   `INTERNAL\sa_repository`.

The chat agent itself was not driven: the sandbox's Anthropic key had no
credit. The Queries editor exercises the same client construction
(`DataSourceService.construct_clients`) the agent uses.

## The fix

- `Membership.custom_attributes` (migration `identmap01`): admin-only key/values
  set on invite, member edit and CSV import (`attr.<key>` columns, merged);
  readable only with `manage_members`; never rendered into the prompt;
  `membership.attr:<key>` resolves admin value first, then IdP-synced
  (`services/member_attributes.py`).
- Registry `IdentitySlot` + `Connection.identity_mapping`
  (`services/connection_identity_mapping.py`): slot → whitelisted source,
  resolved per signed-in member in the credential resolvers
  (`ConnectionService.resolve_credentials`,
  `DataSourceService.resolve_credentials[_for_connection]`), before base
  credentials; any empty slot → 403 `identity_mapping.unresolved`; a mapped
  resolve counts as a *user* identity for catalog writes; custom tables /
  accelerated copies are off while a mapping is active.
- Postgres slot `session_role`: `-c role=<role>` in the startup packet (part of
  the pool key), strict role-name charset, role-switch SQL refused by the
  client and by the generated-code validator.
- Qlik slots `user_directory`, `user_id`; the member-typed variant is removed;
  identity properties are read-only. A legacy `user_required` Qlik connection
  fails closed with an actionable message until a mapping is saved, which
  turns it into `system_only` + mapping.

## What this proves / regression notes

- Proves per-member identity end to end on a real Postgres and on the Qlik QRS
  wire format, including fail-closed behavior and that indexing keeps the
  connection's own identity.
- Known limit: the shared catalog is indexed with the connection's identity,
  so table/app *names* are visible to every member even where their mapped
  identity cannot read the data.
- Pre-existing, unrelated: `tests/e2e/test_connection_file_browse.py::
  test_contract_sharepoint_server` fails with `KeyError: 'files'` on `main`
  too (verified in a clean worktree).
