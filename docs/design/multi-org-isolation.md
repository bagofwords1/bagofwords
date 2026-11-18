# Multiple Organizations on One Deployment — Plan and Working Notes

**Status:** Implemented in one change set; in review
**Date:** 2026-11-18
**Tracking:** Linear, team Bow, BOW-156 to BOW-196

This file is the plan and the team's scratchpad while the work is built. Linear
is the source of truth for status and for the detailed findings. This file holds
the phases, the decisions, the compatibility ledger and working notes.

> **Do not put exploit detail in this file.** The repository is public. Each
> finding's impact, location and evidence live in its Linear issue. Titles here
> are deliberately neutral. Once a fix has shipped in a release, its detail may
> be added here.

---

## 1. Goal

Customers want one self-hosted deployment with an organization per department,
each with its own connectors, agents, members and admins, all signing in through
the same identity provider (for example Entra ID).

**Positioning:** organizations are separate workspaces for teams within one
company. For legal or regulatory separation between entities, run separate
deployments.

**Why one deployment and not separate instances:** this is the department case,
the same one Grafana organizations, Tableau sites and ServiceNow domain
separation serve. Separate instances stay the documented answer for hard
isolation. We are not building per-organization databases, schemas or
encryption keys.

A two-pass audit at commit `e7e7c7a5` found that row-level separation is
consistently enforced, and that the gaps are at the edges: connectors and
generated code reaching the host, global identity, request fields that accept
ids from another organization, and integration routing.

## 2. The non-breaking rule

Most deployments are self-hosted with a single organization. Every change must
leave them working as they do today. Each change is one of three types, and a
change ships only once its type is known.

| Type | Meaning | Flag or setting |
|---|---|---|
| **A. No-op for one org** | An "and it belongs to this organization" check. With one organization it never excludes anything. | None |
| **B. Hardening for everyone** | Protects single-org deployments too. Designed so legitimate use keeps working. | A `security.*` setting restores the old behaviour, documented as unsafe |
| **C. Multi-org only** | Active only when `allow_multiple_organizations` is on. | Existing flag, plus the license feature for product work |

Rules that follow from this:

- No schema migrations for the security fixes; use joins, not new columns.
- Security fixes (phases 1 to 4) are never gated behind the enterprise license.
- New uniqueness rules apply to new records only. Existing duplicates get a
  warning, not an error.
- A behaviour change that could surprise an existing deployment warns for one
  release before it enforces, and gets a changelog line.

## 3. Phases and Linear mapping

Status values: `todo`, `in progress`, `in review`, `done`. Update a row when
its PR merges. Phases 1 to 4 are independent of each other. Phase 5 should not
be offered to a customer before phases 1 to 3 are done.

### Phase 1 — Server hardening for connectors and generated code · [BOW-156](https://linear.app/bow1/issue/BOW-156)

Applies to every deployment. Type B.

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-162](https://linear.app/bow1/issue/BOW-162) | Lock down file-based connectors (DuckDB, CSV, QVD, PBIX, network-dir, Iceberg) and the SQL guard | B | in review | |
| [BOW-163](https://linear.app/bow1/issue/BOW-163) | Build database connection URLs safely (MySQL, MariaDB, Postgres, then sweep the rest) | B | in review | |
| [BOW-164](https://linear.app/bow1/issue/BOW-164) | Harden Git clones and reject symlinks during indexing | B | in review | |
| [BOW-165](https://linear.app/bow1/issue/BOW-165) | Run the demo dataset generator in the code sandbox | B | in review | |
| [BOW-166](https://linear.app/bow1/issue/BOW-166) | Confine LibreOffice document conversion | B | in review | |
| [BOW-167](https://linear.app/bow1/issue/BOW-167) | Connector config hardening (Iceberg, SQLite, egress checks, MSSQL, Vertica) | B | in review | |

### Phase 2 — Identity and accounts · [BOW-157](https://linear.app/bow1/issue/BOW-157)

A user is one global account with a membership per organization.

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-168](https://linear.app/bow1/issue/BOW-168) | Sign-in: stop linking provider accounts by email alone | B | in review | |
| [BOW-169](https://linear.app/bow1/issue/BOW-169) | Self-service profile update: remove email and password from it | B | in review | |
| [BOW-170](https://linear.app/bow1/issue/BOW-170) | Invites: registration claims only the invite that was presented | A | in review | |
| [BOW-171](https://linear.app/bow1/issue/BOW-171) | SCIM: keep writes inside the provisioning organization | A | in review | |
| [BOW-172](https://linear.app/bow1/issue/BOW-172) | Members: adding an existing user needs consent; scope what member lists return | A | in review | |
| [BOW-173](https://linear.app/bow1/issue/BOW-173) | Password login alongside SSO: restrict break-glass and local passwords | C | in review | |

### Phase 3 — Validate referenced ids against the caller's organization · [BOW-158](https://linear.app/bow1/issue/BOW-158)

All type A. Each fix lands with a cross-org test.

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-174](https://linear.app/bow1/issue/BOW-174) | Agent tool `read_resources`: retire it or scope it | A | in review | |
| [BOW-175](https://linear.app/bow1/issue/BOW-175) | "Public" data sources: public to their own organization's members only | A | in review | |
| [BOW-176](https://linear.app/bow1/issue/BOW-176) | @-mentions: validate mentioned ids | A | in review | |
| [BOW-177](https://linear.app/bow1/issue/BOW-177) | Completions: drop `widget_id`, scope `step_id` to the report | A | in review | |
| [BOW-178](https://linear.app/bow1/issue/BOW-178) | Instructions: validate references and `primary_instruction_id` | A | in review | |
| [BOW-179](https://linear.app/bow1/issue/BOW-179) | LLM settings and evals: validate model ids and build ids | A | in review | |
| [BOW-180](https://linear.app/bow1/issue/BOW-180) | Reports: validate share recipients, platform id and fork organization | A | in review | |
| [BOW-181](https://linear.app/bow1/issue/BOW-181) | Smaller id lookups (attachments, tool executions, file cache key, principal ids) | A | in review | |
| [BOW-182](https://linear.app/bow1/issue/BOW-182) | Delete the legacy artifact confirm route and its frontend call sites | A | in review | |

### Phase 4 — Integrations routing and membership lifecycle · [BOW-159](https://linear.app/bow1/issue/BOW-159)

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-183](https://linear.app/bow1/issue/BOW-183) | Slack: route by platform, require signatures on the HTTP route | A | in review | |
| [BOW-184](https://linear.app/bow1/issue/BOW-184) | Teams and WhatsApp: route by platform, remove the fallback | A | in review | |
| [BOW-185](https://linear.app/bow1/issue/BOW-185) | Personal API keys: bind to the organization they were created in | C | in review | |
| [BOW-186](https://linear.app/bow1/issue/BOW-186) | Notifications: filter the inbox by organization | A | in review | |
| [BOW-187](https://linear.app/bow1/issue/BOW-187) | Member removal: clean up roles, grants and keys; honour removed memberships everywhere | A | in review | |
| [BOW-188](https://linear.app/bow1/issue/BOW-188) | Webhook triggers: check membership at fire time, scope operations to the organization | A | in review | |

### Phase 5 — Multi-organization as a supported mode · [BOW-160](https://linear.app/bow1/issue/BOW-160)

Type C, and enterprise-licensed (see decisions D3 and D4).

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-189](https://linear.app/bow1/issue/BOW-189) | Superadmin through `BOW_SUPERUSER_EMAILS`; organization creation restricted to superadmins | C | in review | |
| [BOW-190](https://linear.app/bow1/issue/BOW-190) | "Create organization" in the top-left menu, with a dialog (blocked by BOW-189) | C | in review | |
| [BOW-191](https://linear.app/bow1/issue/BOW-191) | Entra/OIDC: operator-owned mapping from groups to organizations; fix sync for people in two organizations | C | in review | |
| [BOW-192](https://linear.app/bow1/issue/BOW-192) | Remaining single-organization assumptions | C | in review | |
| [BOW-193](https://linear.app/bow1/issue/BOW-193) | Docs page and release notes | — | in review | |

### Phase 6 — Central enforcement and tests · [BOW-161](https://linear.app/bow1/issue/BOW-161)

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-194](https://linear.app/bow1/issue/BOW-194) | Query-level guard for org-scoped tables (warn first, enforce later) | B | in review | |
| [BOW-195](https://linear.app/bow1/issue/BOW-195) | Cross-organization regression suite in CI | — | in review | |
| [BOW-196](https://linear.app/bow1/issue/BOW-196) | Permission decorators: organization check on resource ids | A | in review | |

BOW-195 tracks the shared fixture and coverage. Each fix in phases 2 to 4 lands
with its own case from that issue's list, so the suite grows with the fixes
rather than after them.

## 4. Decisions

| # | Date | Decision | Why |
|---|---|---|---|
| D1 | 2026-11-18 | One deployment with multiple organizations is a supported mode. Separate deployments remain the answer for hard isolation. | Matches the department use case and how comparable products handle it. Avoids a breaking tenancy migration. |
| D2 | 2026-11-18 | Enforce the organization boundary centrally (phase 6) in addition to fixing individual gaps. | Nearly every gap was one query or field that missed the check. |
| D3 | 2026-11-18 | Only a superadmin can create an organization. Superadmins are named in the `BOW_SUPERUSER_EMAILS` environment variable (comma-separated), applied at startup and at sign-in. | Simple for operators, no UI and no database edit. Removing an address removes the role. |
| D4 | 2026-11-18 | Creating an additional organization requires the enterprise license (a license feature such as `multi_org`). The first organization on a new deployment stays free. Existing extra organizations on unlicensed deployments keep working. | Product work is licensed; nothing that exists today breaks. |
| D5 | 2026-11-18 | Security fixes (phases 1 to 4) ship to every deployment, licensed or not. | They protect single-org deployments too. |
| D6 | 2026-11-18 | Organization creation lives in the top-left logo menu as a "Create organization" item that opens a dialog. It is shown only to a superadmin on a licensed deployment, including when only one organization exists. The separate organizations list page is deferred. | The menu already lists the user's organizations. Smallest UI that does the job. |
| D7 | 2026-11-18 | The UI term is "Organization", not "Workspace". | Matches the switcher, the role names and the API. "Workspace" collides with "Slack workspace". The "Workspace name" label in General settings should be aligned later. |
| D8 | 2026-11-18 | Placing people into organizations is operator-owned: a mapping from identity-provider groups to organizations in `bow-config.yaml`. Domain auto-join is not the mechanism for departments. | Departments share one email domain. |
| D9 | 2026-11-18 | The creator of an organization joins it as admin by default, as today. An optional first-admin email sends an invite. | Without a list page, a superadmin who does not join could not see the organization. |
| D10 | 2026-11-18 | Linking a first sign-in to an existing account by email keeps its old behaviour on single-organization deployments. The strict rule (one provider, address not marked unverified, not a multi-tenant Microsoft endpoint) applies only when multiple organizations are enabled. `trust_email_for_linking` overrides either way. | A compatibility review showed the strict rule broke local users moving to SSO and SCIM-created users on existing deployments. |
| D11 | 2026-11-18 | Addresses in `BOW_SUPERUSER_EMAILS` are reserved: an account for one can be created only by first-run bootstrap, an invite from a superadmin, or a trusted sign-in provider. Organizations and their SCIM connections cannot add, change or deactivate a superadmin's account. | The role goes to whoever holds the account. |
| D12 | 2026-11-18 | With multiple organizations enabled, an existing account always joins another organization by invite. With one organization, re-adding a removed person attaches them directly, as before. | Consent for shared accounts without changing single-organization behaviour. |
| D13 | 2026-11-18 | Removal through the members page, the API or SCIM deletes the person's roles, grants and API keys in that organization. Removal by LDAP sync does not; the retired membership alone blocks access, and a re-add restores everything. | A directory glitch must not strip custom roles. |
| D14 | 2026-11-18 | The query guard ships switched off (`security.org_scope_guard: off`). The test suite runs it in `warn` mode. `enforce` waits until its review list is triaged. | Any ORM-level hook costs 10 to 15 microseconds per statement; the first run listed 216 statements, mostly children loaded through an already-checked parent. |
| D15 | 2026-11-18 | File connections keep working at any location that is not the app's own state, secrets or system folders. The session is limited to the connection's own files; SQL may only name the connection's own tables. | Existing connections must survive an upgrade. |
| D16 | 2026-11-18 | License seat caps stay per organization. LDAP stays single-organization. One encryption key for the deployment. Preset LLM keys are still copied into each new organization. | Unchanged by design in this change set; documented in the guide. |

## 5. Compatibility ledger

Every new setting and every behaviour change, with its default. This becomes the
source for the changelog entries (BOW-193). Add a row when a PR introduces one.

### New settings

All are optional. With none of them set, a single-organization deployment behaves as before.

| Setting | Default | Introduced by | Notes |
|---|---|---|---|
| `BOW_SUPERUSER_EMAILS` / `auth.superuser_emails` | empty | BOW-189 | Comma-separated emails. Authoritative when set; the environment variable replaces the YAML list. |
| License feature `multi_org` | Enterprise tier | BOW-189 | Gates creation of an additional organization. |
| `oidc_providers[].org_mapping` | empty | BOW-191 | Objects `{group, organization, role}` or `"<group>:<organization>:<role>"`. Roles: `member`, `admin`. |
| `oidc_providers[].trust_email_for_linking`, `google_oauth.trust_email_for_linking` | unset (automatic) | BOW-168 | See D10. |
| `security.unsafe_allow_unrestricted_file_connectors` (`BOW_UNSAFE_ALLOW_UNRESTRICTED_FILE_CONNECTORS`) | `false` | BOW-162 | Restores unrestricted file connections. Unsafe. |
| `security.file_connector_allowed_roots` | unset | BOW-162 | Optional allow-list of folders for file connections. |
| `security.organization_file_roots` | empty | BOW-192 | Folders reserved for the listed organization. |
| `security.office_convert_confinement` | `true` | BOW-166 | Confine document conversion where the host supports it. |
| `security.slack_require_signed_events` | `false` | BOW-183 | Refuse unsigned Slack events over HTTP even for a legacy single platform. |
| `security.org_scope_guard` (`BOW_ORG_SCOPE_GUARD`) | `off` | BOW-194 | `warn` or `enforce`. Developer diagnostic. |

New endpoint: `POST /api/invites/accept`. New `whoami` fields: `can_create_organization`, `is_instance_settings_admin`.

### Behaviour changes to announce

Every deployment:

| Change | Who notices | Linear |
|---|---|---|
| SQL on DuckDB, CSV, QVD, PBIX connections may name only the connection's own tables; file-reading functions and paths as tables are refused | Anyone who queried files directly through such a connection | BOW-162 |
| A file connection may not point at the app's own folders, secrets or system folders | A connection configured on such a location | BOW-162 |
| SQLite connections open read-only | None expected | BOW-167 |
| Options written after a database name still apply, except ones that read local files or redirect the connection (dropped with a warning) | Connections using such options | BOW-163 |
| Symlinked files in a Git repository are not indexed | Repositories that rely on symlinks | BOW-164 |
| A user cannot change their own sign-in email | API clients that did | BOW-169 |
| A person removed through the members page, API or SCIM returns with exactly the invited role; their API keys in that organization are revoked | Any deployment | BOW-187 |
| An inbound webhook whose owner has left the organization is skipped until the owner returns | Integrations created by a departed member | BOW-188 |
| Slack HTTP events for a Socket Mode connection are refused | None expected | BOW-183 |
| `security.block_private_outbound`, when already on, also covers Cosmos DB, Couchbase, Iceberg, Azure Blob and Kubernetes endpoints and SQL Server failover hosts | Deployments with that setting and such a private connection | BOW-167 |

Only with `allow_multiple_organizations` on:

| Change | Linear |
|---|---|
| Creating an organization needs a superadmin and the license feature | BOW-189 |
| License, system logs, product updates and embedding rules need a superadmin | BOW-189 |
| A personal API key works only in the organization it was created in | BOW-185 |
| An existing account joins another organization by invite | BOW-172 |
| A first sign-in links to an existing account by email only from a trusted provider; an untrusted provider cannot create the account of an invited address | BOW-168 |
| Emergency password sign-in in `sso_only` mode is limited to superadmins | BOW-173 |
| A sign-up domain is assigned by a superadmin and belongs to one organization | BOW-191 |
| Forcing a sign-out of someone who also belongs to another organization needs a superadmin | BOW-172 |
| One Slack workspace, Teams bot or WhatsApp number per organization for new connections | BOW-183, BOW-184 |

## 6. Guidance for customers before the fixes ship

- PostgreSQL, the Docker image with `ENVIRONMENT=production`, Linux kernel 6.7 or newer.
- Set `security.block_private_outbound` and `security.network_dir_allowed_roots`.
- One sign-in provider, a single-tenant Entra app, `auth.mode: sso_only`.
- Keep uninvited sign-ups and domain auto-join off.
- Give connection-creation rights only to trusted admins.
- Connect each Slack workspace or Teams tenant to one organization only.

## 7. Open questions

- [x] License feature: `multi_org`, Enterprise tier.
- [x] License seat and agent caps stay per organization (D16).
- [x] LDAP stays single-organization (D16).
- [x] `read_resources` is kept and scoped to the run's organization and data sources (BOW-174).
- [x] Force sign-out of a shared account needs a superadmin on multi-org (BOW-172).
- [x] The group mapping removes memberships it created when the person leaves every mapped group (BOW-191).
- [ ] Query guard: triage the review list, then enable `enforce` (BOW-194).
- [ ] Connections with no credentials of their own use the app's AWS credentials from the environment. Decide whether to gate this like the Google default credentials.
- [ ] Preset LLM keys are copied into every new organization; make this optional.
- [ ] Encryption key ids for rotation.
- [ ] A skipped webhook delivery is not shown in the trigger's run history.
- [ ] Organizations list page for superadmins: when, and what it shows. (deferred from BOW-190)
- [ ] Align "Workspace name" in General settings with "Organization". (D7)

## 8. Working notes

Scratchpad. Add dated notes under the phase you are working on: what you tried,
what broke, what to test. Keep exploit detail in Linear.

### Phase 1

- 2026-11-18: The DuckDB lockdown must allow the connection's own configured
  paths rather than turn file access off, because connections are views that
  read lazily. Checked on DuckDB 1.5.4 with local files only. Still to test
  before merge: S3, GCS and Azure paths, and each of the six clients.

### Phase 2

- 2026-11-18 (after PR review): an expired invite is never claimed, by
  registration, by sign-in or by `POST /api/invites/accept`. An invite link
  opened by someone who already has an account is carried through password or
  SSO sign-in and accepted afterwards. Known limit: a `redirect=` target on the
  sign-in URL is dropped on that path.

### Phase 3

- 2026-11-18: The legacy artifact confirm route has four frontend call sites and
  no others in `integrations/`, `backend/app/static` or `examples/`.

### Phase 4

- 2026-11-18: A share list no longer shows a person who has left the
  organization. The dashboards golden baseline
  (`test_visibility_baseline[owner]`) still records the old listing. Open
  decision: regenerate the baseline or restore the old listing.

### Phase 5

- 2026-11-18: The top-left menu already lists the user's organizations
  (`frontend/layouts/default.vue`, the workspace dropdown). The existing
  first-run page `/organizations/new` stays as it is.

### Phase 6

- 2026-11-18: The dashboards corpus test fixture fails on the main branch (it replays downgrades twice now that the head is `worldai01`). Unrelated to this work; needs its own fix.
- 2026-11-18 (after PR review): group mapping never removes on doubt. When the
  directory cannot resolve a group's display name, rules that map by name keep
  their memberships until a later sign-in; rules that map by object id are not
  affected. Deleting a rule, the last one included, removes the memberships it
  created at each person's next sign-in.
- 2026-11-18: Quoted schema and table names that exist in the DuckDB session
  are accepted by the relation check.
- 2026-11-18: CI on the PR. Unit, frontend unit and data-source integration
  jobs pass. The dashboards e2e jobs fail at fixture setup (a migration
  downgrade inherited from main), so CI has not run the dashboards tests on
  this branch. The other e2e jobs fail only on a 2-second timing budget
  (`test_indexing_happy_path`; the request takes about 1.5 s on main and on
  this branch) and one worker crash inside pandas.
- 2026-11-18: Another open PR also bumps `VERSION` to 0.0.629. Whichever merges
  second must take the next number and move its changelog entry.
