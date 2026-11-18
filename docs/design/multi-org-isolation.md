# Multiple Organizations on One Deployment — Plan and Working Notes

**Status:** Planning complete; no code written yet
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
| [BOW-162](https://linear.app/bow1/issue/BOW-162) | Lock down file-based connectors (DuckDB, CSV, QVD, PBIX, network-dir, Iceberg) and the SQL guard | B | todo | |
| [BOW-163](https://linear.app/bow1/issue/BOW-163) | Build database connection URLs safely (MySQL, MariaDB, Postgres, then sweep the rest) | B | todo | |
| [BOW-164](https://linear.app/bow1/issue/BOW-164) | Harden Git clones and reject symlinks during indexing | B | todo | |
| [BOW-165](https://linear.app/bow1/issue/BOW-165) | Run the demo dataset generator in the code sandbox | B | todo | |
| [BOW-166](https://linear.app/bow1/issue/BOW-166) | Confine LibreOffice document conversion | B | todo | |
| [BOW-167](https://linear.app/bow1/issue/BOW-167) | Connector config hardening (Iceberg, SQLite, egress checks, MSSQL, Vertica) | B | todo | |

### Phase 2 — Identity and accounts · [BOW-157](https://linear.app/bow1/issue/BOW-157)

A user is one global account with a membership per organization.

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-168](https://linear.app/bow1/issue/BOW-168) | Sign-in: stop linking provider accounts by email alone | B | todo | |
| [BOW-169](https://linear.app/bow1/issue/BOW-169) | Self-service profile update: remove email and password from it | B | todo | |
| [BOW-170](https://linear.app/bow1/issue/BOW-170) | Invites: registration claims only the invite that was presented | A | todo | |
| [BOW-171](https://linear.app/bow1/issue/BOW-171) | SCIM: keep writes inside the provisioning organization | A | todo | |
| [BOW-172](https://linear.app/bow1/issue/BOW-172) | Members: adding an existing user needs consent; scope what member lists return | A | todo | |
| [BOW-173](https://linear.app/bow1/issue/BOW-173) | Password login alongside SSO: restrict break-glass and local passwords | C | todo | |

### Phase 3 — Validate referenced ids against the caller's organization · [BOW-158](https://linear.app/bow1/issue/BOW-158)

All type A. Each fix lands with a cross-org test.

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-174](https://linear.app/bow1/issue/BOW-174) | Agent tool `read_resources`: retire it or scope it | A | todo | |
| [BOW-175](https://linear.app/bow1/issue/BOW-175) | "Public" data sources: public to their own organization's members only | A | todo | |
| [BOW-176](https://linear.app/bow1/issue/BOW-176) | @-mentions: validate mentioned ids | A | todo | |
| [BOW-177](https://linear.app/bow1/issue/BOW-177) | Completions: drop `widget_id`, scope `step_id` to the report | A | todo | |
| [BOW-178](https://linear.app/bow1/issue/BOW-178) | Instructions: validate references and `primary_instruction_id` | A | todo | |
| [BOW-179](https://linear.app/bow1/issue/BOW-179) | LLM settings and evals: validate model ids and build ids | A | todo | |
| [BOW-180](https://linear.app/bow1/issue/BOW-180) | Reports: validate share recipients, platform id and fork organization | A | todo | |
| [BOW-181](https://linear.app/bow1/issue/BOW-181) | Smaller id lookups (attachments, tool executions, file cache key, principal ids) | A | todo | |
| [BOW-182](https://linear.app/bow1/issue/BOW-182) | Delete the legacy artifact confirm route and its frontend call sites | A | todo | |

### Phase 4 — Integrations routing and membership lifecycle · [BOW-159](https://linear.app/bow1/issue/BOW-159)

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-183](https://linear.app/bow1/issue/BOW-183) | Slack: route by platform, require signatures on the HTTP route | A | todo | |
| [BOW-184](https://linear.app/bow1/issue/BOW-184) | Teams and WhatsApp: route by platform, remove the fallback | A | todo | |
| [BOW-185](https://linear.app/bow1/issue/BOW-185) | Personal API keys: bind to the organization they were created in | C | todo | |
| [BOW-186](https://linear.app/bow1/issue/BOW-186) | Notifications: filter the inbox by organization | A | todo | |
| [BOW-187](https://linear.app/bow1/issue/BOW-187) | Member removal: clean up roles, grants and keys; honour removed memberships everywhere | A | todo | |
| [BOW-188](https://linear.app/bow1/issue/BOW-188) | Webhook triggers: check membership at fire time, scope operations to the organization | A | todo | |

### Phase 5 — Multi-organization as a supported mode · [BOW-160](https://linear.app/bow1/issue/BOW-160)

Type C, and enterprise-licensed (see decisions D3 and D4).

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-189](https://linear.app/bow1/issue/BOW-189) | Superadmin through `BOW_SUPERUSER_EMAILS`; organization creation restricted to superadmins | C | todo | |
| [BOW-190](https://linear.app/bow1/issue/BOW-190) | "Create organization" in the top-left menu, with a dialog (blocked by BOW-189) | C | todo | |
| [BOW-191](https://linear.app/bow1/issue/BOW-191) | Entra/OIDC: operator-owned mapping from groups to organizations; fix sync for people in two organizations | C | todo | |
| [BOW-192](https://linear.app/bow1/issue/BOW-192) | Remaining single-organization assumptions | C | todo | |
| [BOW-193](https://linear.app/bow1/issue/BOW-193) | Docs page and release notes | — | todo | |

### Phase 6 — Central enforcement and tests · [BOW-161](https://linear.app/bow1/issue/BOW-161)

| Linear | Work | Type | Status | PR |
|---|---|---|---|---|
| [BOW-194](https://linear.app/bow1/issue/BOW-194) | Query-level guard for org-scoped tables (warn first, enforce later) | B | todo | |
| [BOW-195](https://linear.app/bow1/issue/BOW-195) | Cross-organization regression suite in CI | — | todo | |
| [BOW-196](https://linear.app/bow1/issue/BOW-196) | Permission decorators: organization check on resource ids | A | todo | |

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

## 5. Compatibility ledger

Every new setting and every behaviour change, with its default. This becomes the
source for the changelog entries (BOW-193). Add a row when a PR introduces one.

### New settings

| Setting | Default | Introduced by | Notes |
|---|---|---|---|
| `BOW_SUPERUSER_EMAILS` | empty | BOW-189 | Comma-separated emails. Grants superadmin. |
| License feature `multi_org` (name to confirm) | per license | BOW-189 | Gates creation of an additional organization. |
| OIDC provider `org_mapping` | empty | BOW-191 | `"<group>:<organization id>:<role>"`. Empty means today's behaviour. |
| `security.*` escape hatch for file connectors (name to confirm) | secure | BOW-162 | Restores the old behaviour. Documented as unsafe. |

### Behaviour changes to announce

| Change | Who is affected | Rollout | Linear |
|---|---|---|---|
| Organization creation needs a superadmin and a license | Multi-org deployments where users create their own organizations | Changelog entry | BOW-189 |
| Personal API keys work only in their own organization | Multi-org users who send one key with different organization headers | Warn for one release, then enforce | BOW-185 |
| A removed member who is re-invited gets exactly the invited role | Any deployment | Changelog entry | BOW-187 |
| Files reached through symlinks in a Git repository are no longer indexed | Repositories that rely on symlinks | Changelog entry | BOW-164 |
| Profile update no longer accepts email or password | API clients that use it for that | Check the frontend first; changelog entry | BOW-169 |
| First sign-in no longer links to an existing account by email when more than one sign-in provider is enabled | Deployments with several providers | Operator setting; changelog entry | BOW-168 |
| Some connector options are rejected (Iceberg extra properties, MSSQL parameters) | Connections using unlisted options | Log the rejected key; document the allowlist | BOW-167 |

## 6. Guidance for customers before the fixes ship

- PostgreSQL, the Docker image with `ENVIRONMENT=production`, Linux kernel 6.7 or newer.
- Set `security.block_private_outbound` and `security.network_dir_allowed_roots`.
- One sign-in provider, a single-tenant Entra app, `auth.mode: sso_only`.
- Keep uninvited sign-ups and domain auto-join off.
- Give connection-creation rights only to trusted admins.
- Connect each Slack workspace or Teams tenant to one organization only.

## 7. Open questions

- [ ] Name of the license feature (`multi_org`?) and which tiers include it.
- [ ] License seat and agent caps: instance-wide, or per organization by design? (BOW-192)
- [ ] LDAP: document as single-organization only, or add per-organization admission? (BOW-192)
- [ ] `read_resources`: retire or scope? (BOW-174)
- [ ] Force sign-out by an org admin: per-organization sessions, or superadmin only on multi-org? (BOW-172)
- [ ] Entra mapping: remove a membership when the user leaves the mapped group? (BOW-191)
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

### Phase 3

- 2026-11-18: The legacy artifact confirm route has four frontend call sites and
  no others in `integrations/`, `backend/app/static` or `examples/`.

### Phase 4

### Phase 5

- 2026-11-18: The top-left menu already lists the user's organizations
  (`frontend/layouts/default.vue`, the workspace dropdown). The existing
  first-run page `/organizations/new` stays as it is.

### Phase 6
