# Jira Data Center (on-prem) connector

`jira_dc` connects a self-hosted Jira Data Center or Server, read-only and live.
The agent queries issues and their history **as tables** and reads one issue **as
a document**. Every call runs as one Jira identity, so project permissions and
issue security apply. Design and call budget: `docs/design/jira-data-center-connector.md`.

Jira **Cloud** is not covered here; it still goes through the "Jira / Atlassian" MCP preset.

| | |
|---|---|
| Client | `backend/app/data_sources/clients/jira_dc_client.py` |
| Shared HTTP layer | `backend/app/data_sources/clients/_atlassian_dc_http.py` (also used by `confluence_dc`) |
| Schemas | `JiraDcConfig`, `JiraDcTokenCredentials`, `JiraDcUserPassCredentials` in `backend/app/schemas/data_sources/configs.py` |
| Registry | `"jira_dc"` in `backend/app/schemas/data_source_registry.py` |
| Mock server | `tools/jira/mock_jira_server.py` (`--port 8098 --context /jira`) |
| Tests | `backend/tests/unit/test_jira_dc_client.py`, `backend/tests/e2e/test_jira_dc.py` |
| Feedback loop | `docs/feedback-loops/jira-dc-connector.md` |

## Setting it up

1. **Jira URL** with its context path, e.g. `https://intranet.example.com/jira`.
2. **Projects** (optional): `OPS, HR`. A hard scope for every query, search and read.
3. **Tables** (optional): a subset to expose; blank means all 16.
4. **Max rows** per query (default 2000, up to 10000).
5. **Read issues as documents** (default on): lets the agent search and read issues.
6. **Credentials**: a personal access token (Data Center 8.14+, sent as Bearer;
   required where the site signs in through SSO) or a username and password. Both
   work as the connection's identity or per user.

### Who the agent reads as

- `system_only`: everyone reads as the configured account. Use a dedicated service
  account limited to the projects you mean to share.
- `user_required`: each member connects with their own token or login; it is
  checked once on Save (`GET /rest/api/2/myself`). A refusal is a typed 422
  (`connection.jira_login_rejected`, or `connection.jira_login_captcha` when the
  account is behind a CAPTCHA) and nothing is stored.

## What the agent sees

Tables: `issues`, `issue_changelog`, `issue_comments`, `worklogs`, `issue_links`,
`attachments`, `projects`, `versions`, `components`, `boards`, `sprints`,
`statuses`, `priorities`, `resolutions`, `issue_types`, `fields`. Every custom field
becomes an `issues` column (e.g. "Story Points" → `story_points`, numeric).

A query is a JSON spec; JQL picks the issues and the agent aggregates in pandas:

```json
{"table": "issue_changelog", "jql": "project = OPS AND resolved >= -30d"}
```

Documents: `read_file("issue:OPS-123")` (or `OPS-123`, or a `/browse/OPS-123`
link) returns the header fields, the description (tables kept), every comment,
links and attachment metadata. `search_files` is Jira text search;
`list_files("OPS")` lists a project's recently updated issues.

## On-prem specifics

The same handling as the Confluence Data Center connector: internal CAs through the
deployment's `REQUESTS_CA_BUNDLE`; redirects (SSO) never followed and named; a proxy
login page or wrong context path named as "not the Jira REST API"; CAPTCHA and
disabled tokens named; 429 retried up to twice honouring `Retry-After`, then a typed
throttle error; egress pinning and the 16 MB response cap. Outbound proxies are not
supported yet.

## Limits worth knowing

- Comments and worklogs Jira cut off in a search (it embeds 20) cost one call per
  such issue; more than 100 in one query stops with "narrow the JQL".
- Data Center's search window is 10,000 results per query; beyond it the query
  stops and says so.
- No `users` table: Data Center user search needs a search term. People appear as
  columns on every table instead.
