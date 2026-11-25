# Design — Siemens Teamcenter connector (`teamcenter`)

Status: implemented. Owner area: `backend/app/data_sources/clients/teamcenter_client.py`.

## 1. Problem

Product data in manufacturing companies — parts, assemblies (BOMs), engineering
changes and the specifications attached to them — lives in Siemens Teamcenter, the
PLM system. It was not reachable from Bag of Words at all. Engineers, planners and
quality teams ask questions such as "which parts in the pump assembly are not
released?", "where is this bolt used?", "which open change notices touch the
impeller?" or "what is the design pressure in the pump spec?", and today they answer
them by clicking through Teamcenter.

## 2. Goals and non-goals

Goals
- Read-only, live access to Teamcenter (no warehouse sync, no row index).
- Business objects as **tables** with the site's own (custom) properties, the
  catalog built from Teamcenter's type metadata the way ServiceNow's is built from
  `sys_dictionary`.
- BOM (under a revision rule), where-used and change ↔ part links as tables.
- The documents attached to items (PDF, Office, text) readable as **files**; native
  CAD is never read.
- Sign-in the way Teamcenter sites actually work: **SSO through Teamcenter Security
  Services** (Entra ID or Active Directory behind it), Kerberos, or a Teamcenter
  login — always as the member, so Teamcenter access rules apply per person.
- Tolerance of version differences: Teamcenter's service versions differ between
  releases and there is no public instance to test against.

Non-goals (follow-ups, section 9)
- Writing to Teamcenter. Teamcenter X (cloud, needs Siemens' Extended API add-on).
- An own full-text index over documents (search uses Teamcenter's saved queries).
- Row snapshots of large types for fast filtering.

## 3. Shape

One **hybrid** connection, like `jira_dc` and ServiceNow:

| Capability | What backs it |
|---|---|
| `QUERY` | `execute_query(spec)` → saved query → batched `getProperties` → DataFrame |
| `SEARCH_FILES` | the dataset saved query (`Dataset...`) by name, `*` wildcards |
| `LIST_FILES` | the identity's home folder, a folder, or one revision's attachments |
| `READ_FILE` | dataset → `ref_list` → read ticket → file server download → text |

Tables: one per configured business object type that exists on the server
(`A2_PartRevision` → `a2_part_revision`), plus `bom`, `where_used`, `change_items`
(only when a change type is configured) and `revision_rules`.

## 4. Wire protocol (and where each shape comes from)

`POST {base}/JsonRestServices/<Library>-<YYYY>-<MM>-<Service>/<op>` with
`{"header": {"state": {}, "policy": {}}, "body": {...}}`; the session lives in the
web tier's cookies; an Active Workspace gateway's `XSRF-TOKEN` cookie is echoed as
`X-XSRF-TOKEN`. Failures are exception objects carrying a `.QName`.

| Operation | Service versions tried (newest first) | Source |
|---|---|---|
| login / loginSSO | Core-2011-06-Session, Core-2006-03-Session | Postman capture (login), TC 10.1 services reference (loginSSO) |
| session identity | Core-2007-01-Session/getTCSessionInfo | Postman capture |
| type metadata | Core-2015-10-Session/getTypeDescriptions2, Core-2011-06-Session/getTypeDescriptions | TC 10.1 reference (2011-06); 2015-10 inferred |
| properties | Core-2006-03-DataManagement/getProperties | Postman capture |
| saved queries | Query-2006-03-SavedQuery/getSavedQueries, describeSavedQueries | Postman capture (list); describe inferred |
| run a query | Query-2008-06 / 2007-06 executeSavedQueries, 2006-03 executeSavedQuery | tcsoapy, community reports |
| BOM | Cad-2007-01-StructureManagement createBOMWindows / expandPSOneLevel / closeBOMWindows / getRevisionRules | Mendix connector reference, tcsoapy |
| where-used | Core-2007-01-DataManagement/whereUsed, Core-2012-02 | tcsoapy; bodies inferred |
| documents | Core-2006-03-FileManagement/getFileReadTickets, `{fms}/fmsdownload/<name>?ticket=` | upload side from the Postman capture; download inferred |

The client remembers per server the first version that answers; a missing
operation (404/405/501, or an exception saying the operation is unknown) moves on.
Responses are read leniently (maps as objects or `[[keys],[values]]`, missing
properties as empty values). Inferred shapes are the first thing a real install
can contradict: "Test connection" checks each capability separately (object
types, saved queries, revision rule, documents) and names the one that failed.

## 5. Query model

Saved queries are Teamcenter's only search. An object table uses the first
configured saved query (default `Item Revision...`, then `General...`) that has an
entry for the object type; the entry for each column is found from the query's
description (`attributeName`), falling back to its label. Filters a query entry
can take are sent to Teamcenter (case-insensitive `*` wildcards, matching the
stored value — a user ID for `owning_user`) and are **not** re-checked locally
(the display value read back may differ); every other filter (ranges, empty
values, columns without an entry) is applied here over at most `max_rows`
objects. Rows of a subtype the query also matched are dropped — each type is its
own table.

`bom` opens a window under the rule (`revision_rule` config, overridable per
query), expands level by level in batches of 100 parent lines, reads line
properties (`bl_quantity`, `bl_sequence_no`) **while the window is open** (lines die
with it), and always closes the window. `where_used` walks one level or all;
`change_items` reads the Change Manager relations (problem, impacted, solution,
reference) of one change or of the changes a change-level filter selects (≤ 200).

## 6. Sign-in

| Mode | Scope | How |
|---|---|---|
| `userpass` | system, user | `Session/login` |
| `kerberos` | system | Negotiate to the TcSS login service with BOW's keytab, token read from its redirect, `loginSSO` |
| `kerberos_delegated` | user | the same with the member's delegated credential (S4U2Proxy), from their verified directory identity |
| `oauth` ("Sign in with Teamcenter SSO") | user | the browser flow below |

**Browser SSO.** TcSS is not OAuth: the browser goes to the TcSS login URL with this
application's ID (`appID` by default, configurable), signs in through the identity
provider behind TcSS, and TcSS returns it to the application root URL registered for
that ID — `/api/connections/teamcenter/sso/callback`. TcSS does not carry our state
through, so the signed state (the same JWT the OAuth flow uses) rides in an httponly
cookie set when the sign-in starts (`SameSite=None` over HTTPS, since TcSS may POST
back). The callback reads the token and user from the parameters (configured names,
else the first whose name contains "token" / "user"), stores them as the member's
credential under the `oauth` mode — so the whole per-user machinery (Connect button,
sign-in-required states, roster) applies unchanged — and runs the same verify /
roll back / start-indexing tail as the OAuth callback (`_finish_sign_in`). A
callback without its cookie, with a token Teamcenter refuses, or with no token
stores nothing.

Default member modes (`default_user_auth_modes`): `kerberos_delegated` when the
system identity is Kerberos, `oauth` when an SSO login URL and application ID are
set, otherwise members bring their own Teamcenter login.

**Sessions.** A Teamcenter login starts a server process, so sessions are cached
per identity (an HMAC of the URL, group/role and credential material), serialized
by a lock, restored once when Teamcenter says the session ended, and logged out
when idle 15 minutes (at most 64 cached). A TcSS token is redeemed again when its
session ends; when Teamcenter no longer accepts it the member is asked to sign in
again.

## 7. Security

- Every call runs as the member's (or the service account's) Teamcenter identity.
- Messages leaving the client are fixed strings: no URL, response body or secret.
- HTTPS unless an admin enables "Allow HTTP" for a lab; requests go through the
  pinned egress session; responses and downloads are size-capped.
- Document ids must be well-formed UIDs; only allowed extensions are read.

## 8. Verification

- `backend/tests/unit/test_teamcenter_client.py` and `backend/tests/e2e/test_teamcenter.py`
  run against `tools/teamcenter/mock_teamcenter_server.py` (web tier, TcSS login
  service, file server; a `legacy` profile with only the older service versions).
- `docs/feedback-loops/teamcenter-connector.md`: the live UI loop with a real LLM.

## 9. Follow-ups

- Confirm the inferred shapes against a pilot instance (its `genSoaApi` output via
  Krusty84/TCApi2Postman gives an OpenAPI description of its exact services).
- Teamcenter X sign-in (OIDC with token exchange) once a customer has the add-on.
- Active Workspace full-text search (Finder) where Solr indexing is enabled.
- Optional file content index for ranked document search.
