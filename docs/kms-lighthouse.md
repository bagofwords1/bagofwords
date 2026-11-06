# KMS Lighthouse

`kms_lighthouse` is a read-only knowledge connector over the **KMS Lighthouse
public API**. An agent uses it to search, browse and read KMS knowledge
**articles** (items) and decision-tree **scenarios** through the existing file
tools (`search_files`, `list_files`, `read_file`), the same way the Documentum
connector works. It needs an enterprise license and is marked beta.

There is no catalog and no index: every search, listing and read is a live call
made as one KMS identity, so KMS permissions decide what each person sees.

The connector was built and tested against a mock of the public API PDF. No real
tenant has been contacted; see [Verification boundary](#verification-boundary).

## Setup

Create a connection of type **KMS Lighthouse** and fill in:

| Field (form title) | Config key | Default | Notes |
|---|---|---|---|
| KMS Lighthouse URL | `base_url` | required | The tenant URL, for example `https://kms.example.com`. HTTPS only, unless Allow HTTP is on. It may end with the API prefix (`https://kms.example.com/kms/lh/api`), which is stripped; any other path, a user name or password in the URL, a query or a fragment is refused. |
| API path prefix | `api_prefix` | `/kms/lh/api` | One prefix for every endpoint. Change it only if your tenant serves the API elsewhere. It must be a plain path (no `.` or `..` segment). |
| Layout ID | `layout_id` | blank | Optional. When set, search sends it as `layoutId`, so search is limited to that layout. Browsing and reads ignore it. |
| Root folder ID | `root_folder_id` | blank | Optional. The KMS folder that browsing starts from. It limits **browsing only** (see [Scope](#scope)). Folder id 0 is a valid value. |
| Include offline content | `include_offline` | off | Also show content that is not Online (drafts, offline). |
| Max results | `max_results` | 200 | 1 to 1000. Upper bound on entries returned by one search or one listing. |
| Allow HTTP (test labs only) | `allow_http` | off | Allows plain HTTP for an isolated lab. Use HTTPS with a trusted certificate for customer deployments. |

A hidden `index_mode` field accepts only `none` (live only). The base URL goes
through the platform egress check like every other connector's destination.

**The account.** Ask the KMS administrator for a KMS account that may use the
KMS public API. For a shared (system) connection this is usually a dedicated
service account whose KMS permissions cover the content the agents should see.
Accounts that sign in to KMS only through single sign-on may have no KMS
password; such a person needs the token variant or a separate API account. KMS
also has an "API users" area with a secret key, but that key drives a hash login
that BOW does not support (see [Not supported](#not-supported)).

**Test connection.** For a username and password, the test always logs in fresh
(it never reuses a pooled session), then reads the session identity (optional)
and the first level of the folder tree (the root folder, or the top level). On
success the connection dialog shows the platform's "Connected successfully.
Found N documents." N counts the articles and scenarios (not folders) of one
recursive listing from the root folder or the top level. That listing stops at
200 entries (folders included; Max results when that is lower) or after 40
folder calls, whichever comes first; when it is cut, N is a floor, even well
below 200. When no root folder is set
and the top level comes back empty or is refused, the test fails with a hint to
set Root folder ID or check the account's KMS permissions. With a root folder
set, an empty folder is a pass.

**Indexing.** The registry declares `catalog_ownership="none"`, so a new KMS
connection skips the indexing step and the file count shown for it is always 0.

## Authentication

Two variants, both available for system and per-user connections:

| Variant | Fields | What the connector does |
|---|---|---|
| Username / Password (`userpass`, default) | `username`, `password` | `POST {prefix}/login` with the username and password as query parameters (a call found outside the PDF). The session is whatever the answer carries: its cookies and, when the body holds one, a token sent as `Authorization: Bearer`. The username is trimmed; the password is used exactly as typed. |
| API token (Bearer) (`token`) | `api_token` | The pasted token is sent as `Authorization: Bearer` on every call. It never logs in and cannot be renewed: a 401 means the token is wrong or no longer valid. The token may contain only visible ASCII characters (letters, digits, punctuation, no spaces); surrounding blanks are trimmed. |

When `auth_type` names a variant whose fields are present, that variant is
used; otherwise a present token wins, then a username and password.

Failure messages are fixed texts that never carry a URL, a query string, a KMS
response body or a credential. They name what to do for both kinds of
connection ("If this connection uses each person's own KMS account, that person
should reconnect it.").

**Per-user (member) credentials.** On a connection whose auth policy is
"each user signs in", every member saves their own KMS login or token from the
credentials dialog. It is stored on the connection
(`POST /api/connections/{id}/my-credentials`) and only the variant's own fields
are kept. Two typed errors are specific to KMS:

- `connection.kms_token_format` (422): a pasted token that is not plain visible
  ASCII, on save and on the member credential test.
- `connection.kms_use_connection_credentials` (400): the legacy data-source
  route `POST /api/data_sources/{id}/my-credentials` refuses KMS, because the
  runtime reads only the connection-level row. The legacy
  `DELETE /api/data_sources/{id}/my-credentials` also clears the
  connection-level row, so a Disconnect from either place really disconnects.

The member credential test (`POST /api/data_sources/{id}/my-credentials/test`)
builds the test client from the validated fields only, so extra keys in the
request (for example a different `base_url` or `api_prefix`) are dropped for
KMS.

## Who the agent runs as

| Connection | Person | Runs as |
|---|---|---|
| System (shared) | anyone with access | the connection's account or token |
| Each user signs in | a member with saved credentials | that member's own KMS login or token |
| Each user signs in | a member without saved credentials | nobody: the file tools and the Files tab ask the member to connect, and no KMS call is made |
| Each user signs in | an org admin without saved credentials | the connection's system credentials |
| Each user signs in | the data-source owner without saved credentials | intended: the connection's system credentials (see the owner bug below) |

The owner/admin fallback is existing platform behaviour for every connector
(`ConnectionService.resolve_credentials`), not something this connector adds.
On a per-user connection it means that an org admin, or the data-source owner,
who has not saved their own login reads with the connection's account, not with
their own KMS permissions. Save your own login to read as yourself.

**Owner bug (pre-existing, every connector).** For an owner who is **not** an
org admin, the owner branch of that fallback does not fire today, so such an
owner gets the Connect prompt instead. The test that pins the intended
behaviour is marked as a strict `xfail` until a platform fix is decided. See
"Known limitations and follow-ups" below.

When a KMS connection, or the agent that holds it, has been turned off after
failed connection tests, the file tools say so by name and tell the user to ask
an admin to test it, instead of a generic error.

## Scope

What one identity can see is decided by KMS: its permissions, plus the optional
Layout ID for search. The Root folder ID limits **browsing only**:

- `list_files` and the Files tab start at the root folder, and a `folder:<id>`
  outside the root is refused ("Folder is outside the configured root folder.").
- `search_files` covers everything the identity can see (in the layout, when one
  is set), and `read_file` reads any article or scenario id the identity may
  open, also outside the root folder.

Reads are not bounded by the root because the KMS API cannot filter search by
folder or resolve an item's folders cheaply. The agent's planner is told this in
its file-scope note, and the connection is not presented to it as a scope that
denies everything else.

## Browsing by folder path

The agent's live `list_files` result contains articles and scenarios only (no
folder rows), so the agent drills into a folder by its **path**:

- Every entry has a `path` made of folder names from the root folder (or the
  top level), then `<title>.txt`. Example: `Folder/Subfolder/Reset a password.txt`.
- To open a folder, pass the folder part of an entry's path as `folder_id`
  (`folder_id='Folder/Subfolder'`), with `recursive=true` to include subfolders.
  Paths start below the configured root folder when one is set.
- `folder:<digits>` is also accepted (direct callers, and the folder entries the
  Files tab receives).
- `list_files` with no folder is recursive by default; with a folder it lists
  one level unless `recursive=true`.
- Each title becomes one path segment: markup removed, credentials scrubbed,
  `/` replaced by `-`, control characters removed, at most 200 characters; an
  empty or dots-only title is `untitled`. Two sibling folders with the same
  name get `<name> [<id>]` segments, so every path resolves to exactly one
  folder. A path never reaches a URL: it is checked (at most 25 segments) and
  resolved one tree level per segment.
- Ids are typed: `item:<digits>`, `scenario:<digits>`, `folder:<digits>`
  (1-12 digits). Bare digits read as an article; when no article has that id,
  the read tries a scenario once and labels it `scenario:<digits>` in its
  header. An explicit `item:` id never switches kind.
- Renaming a folder in KMS changes its path.

The planner note the agent receives says, for example: "To open a folder, pass
the folder part of an entry's path as folder_id (for example 'Folder/Subfolder')
with recursive=true; paths start below the configured root folder when one is
set, and folder ids are not shown."

## Online and offline content

KMS marks content Online with status 2 (`statusCode`, `status_id` or `status`,
first one present; `2` or `online`). By default only Online content is shown:
offline articles, scenarios and folders are hidden from search and listings,
and reading an offline article or scenario answers "Not found in KMS
Lighthouse, or not available to this identity." A record with no status field
is shown, and its read checks the status again.

With `include_offline` on, offline content is listed, searched and read too,
and each entry carries `status` `online` or `offline`.

## Limits

| Limit | Value | What happens past it |
|---|---|---|
| Entries per search or listing | `max_results` (default 200, 1-1000), lowered by the tool's own `limit` | Search returns what it found and marks the result **incomplete** (`stop_reason` `max_results`). A listing **stops silently**: neither the `list_files` tool nor the Files tab says it was cut. |
| Search paging | pages of 50, at most 20 pages | More results may exist; the search is marked incomplete. |
| Search query | 200 characters (control characters removed) | The rest is cut, not refused. A 400 from KMS says the query may be too long or the Layout ID wrong. |
| Tree calls per `list_files` call | 40 folder calls, path resolution included | The listing stops silently; a folder that cannot be reached within the bound answers "Folder not found, or beyond the browse limit". |
| Folder path | 25 segments of at most 200 characters each | Refused before any request. |
| Scenario tree | 400 entries (steps, option lines and markers), 40 step fetches, depth 25 | A `(not shown: tree truncated)` marker stands where steps were cut, and the tree ends with `[tree truncated: some steps not shown]`. |
| One HTTP answer | 16 MB, timeouts 10 s (connect) and 60 s (read) | "The KMS Lighthouse response was too large." / "could not be reached." |

The planner note states the listing cap and the 40-call bound, and that a short
listing can be incomplete, so the agent is told to open a specific folder by
its path or to use `search_files`. A search stopped by the tool's deadline or a
cancel keeps the entries found so far.

## Sessions and the login hold

Every tool call builds a new client, so a username/password connection would
log in on every call. Instead, each server process keeps an in-memory session
per identity (base URL, prefix, username, password):

- A session is reused for up to 600 seconds. Nothing is written to disk or the
  database; the pool key is an HMAC with a per-process random secret, not the
  password or a hash of it. The pool holds at most 256 identities; idle ones are
  forgotten first.
- When KMS answers a call with 401 or a redirect, the connector logs in once
  more and retries once. If the fresh session is refused too, the message says
  KMS accepted the login but refused the session (ask the KMS administrator
  whether the account may use the API), not that the password is wrong.
- When KMS **rejects a login** (400, 401, 403, or an answer that is not
  `SUCCESS`), no new login with the same username and password is sent for 300
  seconds, from any client in that process. Calls in that window fail at once
  with the login message plus "New login attempts with this account are paused
  for a few minutes." This keeps a wrong password from locking the KMS account
  through retries or the status sweep. Saving a corrected password is a new
  identity and logs in at once.
- **Test connection always logs in fresh**, but it honours the hold. So a
  password changed on the KMS side keeps working for tool calls for up to 600
  seconds in a process that holds a session, while the test reports the real
  state at once.
- A login answer with neither a cookie nor a token is not a session; the
  message asks the KMS administrator whether the account may use the API.
- Each worker process holds its own sessions. No logout is sent (no logout
  endpoint is documented), so every fresh test login leaves one KMS session to
  expire on the KMS side. The platform re-tests system-only connections about
  every 300 seconds; whether that hits a tenant's concurrent-session limit is a
  live-probe item.

The token variant uses none of this: the token is sent as is.

## What article text is used

An article read (`read_file` with `item:<id>`) starts with header lines: Title,
Kind (`article (<sub-type>)`), Version, Language, Updated (ISO date), Summary.
Then the body, in one of two ways:

1. **AI-ready text.** When `/public/item` gives a version and a language, the
   connector asks `ai_answers_item` for the plain text KMS prepares for AI use,
   grouped by tab caption and element caption. It shows **only the elements
   whose names `/public/item` also returned** for the same identity (that answer
   is the permission-filtered view); a repeated element name is shown once.
   Plain AI text is shown as written (scrubbed). An element whose AI text looks
   like markup (anything tag-shaped or a character reference) or is empty is
   rendered from that element's `/public/item` HTML instead.
2. **Item HTML.** When there is no AI text for this read, or none of its
   elements is visible, the `/public/item` elements are converted to plain text
   and grouped by tab, in tab order. Image fields are skipped; an element with
   no known tab goes to a "General" section first.

A failure that concerns only this article's AI text (a 4xx, a plain 500, an
empty or error answer) falls back to the item HTML for this read only; the next
read tries AI text again. A failure that concerns every call (a failed login,
throttling, a redirect, KMS unreachable, a 502/503/504) is reported, never
hidden behind other text.

**Scenarios** (`scenario:<id>`) are requested with `plainText=true` (the PDF
says "content without HTML tags") and rendered as an indented decision tree
under "## Steps": each step's name, text, remark and `Image: <url>` (image token
parameters removed; the image is never fetched), each option as
`- "<label>" ->` with its step one level in. A step seen before renders
`(see step "<name>" above)`; a step that could not be loaded renders
`(step not available)`; when every fetched step failed, the tree starts with
`(some steps could not be loaded)`. Step text is shown as written and scrubbed
for machine credentials; it is not HTML-stripped, because there is no second
source to tell markup from an author's text.

`read_file` returns plain text named `<title>.txt`. `read_raw_bytes` returns the
same text as UTF-8 and honours a byte limit. Reads are always live: KMS content
is never served from the shared read cache.

## Content scrubbing

Everything the connector returns (names, paths, snippets, article and scenario
text) passes through one scrubber. The scope was decided by the product owner
and is recorded in the maintainer notes for this connector
(`onboarding/pr-kms/pr-highlights.md`, kept outside this repository):

- **Machine credentials only.** The connector removes credentials that systems
  put into content: secret URL parameters (also camel-case and compound names,
  for example the file-service token in scenario image URLs), values of secret
  structured keys in JSON, YAML, config, env and header lines, JWTs, `Bearer`
  and `Basic` values, and the password in a `user:password@` URL.
- **Authored text is shown as written.** A password or code that a KMS author
  writes in prose ("Password: 1234", "the default PIN is 0000") is content for
  its readers and is not hidden, the same as every other BOW document connector
  (SharePoint, Drive, OneNote, Documentum return content as is).
- **Command examples are authored text too.** Credentials inside command
  examples (`curl -u`, `wget --password`, `mysql -p`) are shown as written.
  Flag rules could not tell a credential from a host and path or other
  colon-separated values on unknown content. A header, a URL password or a
  secret parameter inside a command still goes by its own rule.
- **Never rewrite legitimate content.** KMS content belongs to many companies
  and is unknown in advance, so over-redaction is treated as a defect.
  Placeholders such as `<your api key>` or `${DB_PASSWORD}` are kept.

The scrubber is defence in depth: the text is already visible to the identity
that reads it in KMS.

## Known scrub limitations

These are known and accepted. Each is described as a class; the exact cases are
listed next to `_scrub` in
`backend/app/data_sources/clients/kms_lighthouse_client.py` and pinned by the
regression corpus in `backend/tests/unit/test_kms_lighthouse_content.py`.

**Credential shapes not caught (the value is shown):**

- Credentials in XML element or attribute form.
- A value separated from its key by a non-ASCII space (the Unicode space
  class), or a key and value split by a line break, a block tag, a separator
  control character, or a closing-then-opening inline tag. Only the part after
  the break shows when a token is cut this way.
- Single-word all-caps keys, plain lower-case session keys, and the
  `Authorization: Token` scheme.
- Bearer values made only of letters, dots and hyphens; a Bearer token opened
  by an angle bracket, a CJK corner bracket or an asterisk, a doubled opener,
  a spaced colon, or a token on the line after a bare "Bearer".
- JWTs whose header does not start like a standard JWT header; fullwidth or
  lookalike key names; a bare secret with no name, separator or scheme next to
  it.
- A token-only URL user part (no `:`), a URL user part split by tags, a URL
  scheme longer than 32 characters, a fully percent-encoded standalone token
  URL, and `token=` or `password=` written outside a URL query.
- A real value shaped like a placeholder (a `$VAR`, a template reference, a
  word placeholder), and values under well-known template roots.
- Some compound "...pass" keys whose word before "pass" is not a known service
  or account word; title-case hyphenated labels.
- Credentials in command-line flags (by design, above), and prose secrets (by
  design).

**Content that may be altered (over-redaction, accepted):**

- A camel-case word that starts like a JWT header; URL parameter names that end
  with a secret word (for example `nextPageToken`, `showPassword`); an
  identifier-shaped quoted key mentioned in prose.
- The word after a secret key or scheme word when it is not a credential (for
  example an unquoted placeholder with spaces loses its first word), bracket or
  quote debris next to a redacted value, and some shell examples where a
  `-u name:group` style argument loses its second part.
- A default value inside a variable reference read after a quoted key in prose.

**Rendering caveats:**

- Inline tags that open right after `:` or `=`, or end right before `@`, are
  joined as the page shows them; a split between two elements becomes a space.
- A zero-width joiner kept inside a word becomes a space in a title or snippet
  (not in an article body).
- AI text: plain text with a literal `&amp;` inside a URL can leave a leftover
  `&amp` after a removed parameter; text that has both a character reference
  and `<placeholder>`-style words renders from the item HTML and loses those
  placeholders; some placeholder-shaped bare tag names count as markup; unknown
  block tags are not detected as markup.
- An empty or markup-only element renders an empty heading; a non-string element
  value renders as written.
- Scenario text is plain text by contract. If a tenant returns HTML despite
  `plainText=true`, tags show as written and a credential split by markup or an
  entity is not scrubbed there (a live-probe item).
- A double quote inside an option label is not escaped; continuation steps of a
  step that also has decisions render after its options.

## Known limitations and follow-ups

Platform findings that are not KMS-specific are analysed in
`onboarding/pr-kms/pr-highlights.md` ("Platform observation"); they are listed
here so they are not lost:

- **Owner fallback bug** (every connector): a data-source owner who is not an
  org admin does not get the owner fallback on per-user connections. Pinned by
  `test_owner_who_is_not_an_admin_runs_as_the_connection_identity` (strict
  `xfail`); awaiting a decision. When the fix lands, remove the mark and confirm
  a real pass (a strict xfail cannot tell a setup failure from the bug).
- **Credential-store split** for Documentum and on-prem SharePoint is unchanged
  (fixed here for KMS only).
- **Creator per-connection grant:** a creator later demoted to member keeps a
  per-connection manage grant (observation; may be intended).
- **Member credential test and auth policy:** the per-user credential test does
  not check the connection's auth policy (observation, every connector).

KMS-specific follow-ups:

- The listing cap and the 40-call bound are silent in the tool output (stated in
  the planner note); search reports "may be incomplete" also when the cap fills
  exactly on a full last page.
- A plain server error (500) while a folder lookup by id opens a hidden
  (offline) branch counts as that branch's failure, so the lookup can answer
  "Folder not found" instead of a server error (never a false "outside the
  root").
- The 400-entry scenario budget is shared across a scenario's options, so a very
  wide first level can crowd out later options.
- Step fetches have no deadline of their own (at most 40 fetches times the read
  timeout).
- The agent-tool helper that names an inactive KMS connection repeats the
  resolver's matching order (drift risk); with several file connections on one
  agent it may pick the first file connection of a matched agent; the
  `group_by='folder'` path returns the raw client error (pre-existing).
- In the credentials dialog, a field-level KMS validation detail can be hidden
  behind the generic validation message.
- Before a PR (P11): replace the placeholder icon with the official logo, run
  the frontend `.mjs` test where `node_modules` exists, have the new error
  translations reviewed by native speakers, and do the live UI pass.

## Not supported

- The hash or secret-key login (`/freshdesk/login`): its algorithm is not
  published. The connector has one place where a login variant plugs in.
- KMS AI answers (`/ai_answers`), suggest, categories and global filters.
- Glossary terms, related items, attachments, and fetching images.
- A catalog or content index: no sample names in the agent prompt, no
  `group_by='folder'`, a file count of 0, and no entry in the integrations
  harness (`backend/tests/integrations/ds_clients.py`, which needs a catalog).
- OAuth or single sign-on, and any write to KMS.

## Privacy

- Request URLs are never logged: the login sends the password in the query
  string, so while a KMS request is in flight urllib3's request lines are
  dropped for that thread only (other connectors' logs are unchanged).
  The connector logs only a fixed operation name, the HTTP status and the
  duration, plus fixed-text lines for the login hold and refusals.
- Error messages are fixed texts with no URL, query, response body, title, id
  or credential; any unexpected failure becomes a fixed "returned an error or
  an unexpected response" message, logged by exception class name only.
- Redirects are never followed; images and links are never fetched; scenario
  image URLs and article text lose their token parameters (see
  [Content scrubbing](#content-scrubbing)).
- Saved credentials are stored encrypted by the platform and are not returned
  by the connection API; validation errors never echo the rejected value.

## Verification boundary

Built and tested against a mock built from the public API PDF (KMS Lighthouse
Public API Documentation 5.0) plus the login call found in a public test
repository; no real tenant was contacted. The mock is
`tools/kms_lighthouse/mock_kms_server.py`
(`python tools/kms_lighthouse/mock_kms_server.py --port 8096`), and it is the
boundary every unit and e2e test mocks. Everything the PDF leaves open (the
prefix per endpoint, cookie or token sessions, the expiry answer, the type
names of scenarios, date order, the query length limit, session limits, AI-text
permissions) is handled defensively and listed as a live-probe item in
`docs/feedback-loops/kms-lighthouse-connector.md`. The UI has not yet had its
live pass on a running stack (P11).
