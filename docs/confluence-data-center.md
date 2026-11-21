# Confluence Data Center (on-prem) connector

`confluence_dc` is a read-only, **live** file connector for a self-hosted
Confluence Data Center or Server. The agent searches, browses and reads pages
and blog posts with the generic file tools (`search_files`, `list_files`,
`read_file`). It is built like KMS Lighthouse: there is no catalog or index,
and every call runs as one Confluence identity, so Confluence's own space
permissions and page restrictions decide what each person sees.

Confluence **Cloud** is not covered here. It still goes through the
"Jira / Atlassian" MCP preset (Atlassian's Rovo MCP server is Cloud-only).

| | |
|---|---|
| Client | `backend/app/data_sources/clients/confluence_dc_client.py` |
| HTML → text | `backend/app/data_sources/clients/_confluence_html.py` |
| Schemas | `ConfluenceDcConfig`, `ConfluenceDcTokenCredentials`, `ConfluenceDcUserPassCredentials` in `backend/app/schemas/data_sources/configs.py` |
| Registry | `"confluence_dc"` in `backend/app/schemas/data_source_registry.py` |
| Mock server | `tools/confluence/mock_confluence_server.py` |
| Tests | `backend/tests/unit/test_confluence_dc_client.py`, `test_confluence_html.py`, `backend/tests/e2e/test_confluence_dc.py` |
| Feedback loop | `docs/feedback-loops/confluence-dc-connector.md` |

## Setting it up

1. **Confluence URL**: the site address *including its context path*, e.g.
   `https://intranet.example.com/confluence`. A pasted `.../rest/api` is
   accepted and dropped. HTTPS is required (Allow HTTP is for isolated labs).
2. **Spaces** (optional): comma-separated space keys (`ENG, HR, ~jdoe`). This
   is a **hard scope**: search adds `space in (...)` to the CQL, browsing never
   leaves these spaces, and reading a page from another space is refused.
   Leave it blank for every space the identity can see.
3. **Credentials**, either:
   * **Personal access token** (Data Center 7.9+), sent as `Authorization: Bearer`.
     This is the recommended option, and the only one that works where the
     site signs in through SSO.
   * **Username and password**, sent as HTTP Basic.

   Both work as the connection's system identity and as per-user credentials.

### Who the agent reads as

* `system_only`: everyone reads as the configured account. The agent can see
  everything that account can see, restricted pages included, so use a
  dedicated service account with only the spaces you mean to share.
* `user_required`: each member connects with their **own** PAT or login
  (`POST /api/connections/{id}/my-credentials`). Page restrictions then apply
  per person. On Save, the login is checked once with
  `GET /rest/api/user/current`. A refused login gets a typed 422 and is not
  stored: `connection.confluence_login_rejected`, or
  `connection.confluence_login_captcha` when the account is locked behind a
  CAPTCHA. If Confluence is unreachable, the login is saved unverified.

### On-prem specifics

| Situation | What the connector does |
|---|---|
| Internal CA / self-signed cert | Trusts the deployment's `REQUESTS_CA_BUNDLE` (the same policy as SharePoint Server and KMS: no per-connection CA path, no "skip verify"). A TLS failure is reported as such. |
| Context path wrong or missing | `/rest/api/user/current` answers 404 or HTML; the test says "not the Confluence REST API… check the context path". |
| SSO in front of Confluence (302 to a login page) | Redirects are never followed; the test says to use a personal access token. |
| CAPTCHA after failed Basic logins (`X-Authentication-Denied-Reason: CAPTCHA_CHALLENGE`) | Named as a CAPTCHA, so the user knows a browser sign-in clears it. The agent is told not to retry. |
| PATs disabled or revoked by the admin | 401, with a message naming that possibility. |
| Rate limiting (DC 8.x+ token bucket, 429) | Up to 2 retries honouring `Retry-After` (capped at 10 s, within the tool's deadline), then a typed throttle error the agent is told not to loop on. |
| Outbound proxy | **Not supported yet**: the session uses `trust_env=False`, like KMS and SharePoint Server. |
| Egress policy | Calls go through `app.core.egress.pinned_session`; `security.block_private_outbound` applies. |

## What the agent sees

* **search_files**: CQL `type in (page, blogpost) [AND space in (...)] AND text ~ "<words>"`
  against `/rest/api/search`. CQL operators in the query are stripped, so any
  text is a valid phrase and cannot widen the space scope. Results carry a
  path (`SPACE/Title`), a highlight-free excerpt and the page link (`web_url`).
* **list_files**: pages space by space, with paths `SPACE/Ancestor/.../Title`.
  Titles are unique per space, so a path (or the shorthand `SPACE/Title`) names
  one page. A `/` inside a title is shown as `∕`, so paths stay unambiguous.
  `folder_id` can be a space key, a path or `page:<id>`.
* **read_file**: `page:<id>`, `blogpost:<id>`, bare digits, or a page URL on the
  same host. The body is `export_view` HTML (macros already expanded, the same
  on every DC version), turned into text: tables become markdown pipe tables,
  code blocks become fenced blocks, and info/note/warning panels are labelled.
  A header gives the title, space, ancestor path, last update (who, version)
  and the link.

Every response is capped at 16 MB with (10 s, 60 s) timeouts. Error messages
are fixed strings that never contain a URL, a response body or a credential.

## Research notes (Nov 2026)

* **REST surface**: Data Center's `/rest/api/content`, `/rest/api/space`,
  `/rest/api/search?cql=` with start/limit paging (`_links.next`), and
  `expand=body.export_view,version,space,ancestors`. The server caps `limit`
  (25 with expanded bodies), so the client always pages.
* **Auth**: PATs since 7.9 (Bearer). Admins can disable them. Basic auth may
  be refused under SSO.
* **Lifecycle**: Server reached end of life in Feb 2024. The current DC LTS is
  10.2, and Atlassian has announced end of life for all DC products on
  28 Mar 2029.
* **HTTP client**: hand-written `requests` code rather than a vendor SDK, to keep
  egress pinning, fixed error texts and the response cap under our control. The
  shared Data Center HTTP layer lives in `_atlassian_dc_http.py` and is also used
  by the Jira Data Center connector.

## Not in this version

* Confluence Cloud as a native type (the v2 API for pages plus v1 for CQL
  search, and the `api.atlassian.com` gateway for scoped tokens and OAuth).
* Attachments and comments.
* A KMS-style on-site diagnostics panel.
* Outbound proxy support.
* Per-agent space lists (a generalised version of the Slack per-agent
  channel setting).
