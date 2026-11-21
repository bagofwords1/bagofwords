# Design — Jira Data Center connector (`jira_dc`)

Status: implemented (beta). Owner area: `backend/app/data_sources/clients/`.

## 1. Problem

Jira was reachable only through the "Jira / Atlassian" MCP preset, which talks to
Atlassian's Cloud-only MCP server. A self-hosted Jira Data Center or Server could
not be connected at all. Even on Cloud, the MCP tools return issues as tool
output, not as tables the agent can count, group and chart.

Who it is for: an org that runs Jira on-prem and wants questions like
"open bugs by assignee", "cycle time of issues resolved last month", "hours
logged per person this sprint" or "what happened on OPS-123?" answered from live
Jira data, with each person seeing only the issues Jira lets them see.

## 2. Goals and non-goals

Goals
- Read-only, live access to Jira Data Center / Server (no warehouse sync, no index).
- Issues and their related records as **tables** the agent aggregates.
- One issue readable as a **document** (description, every comment, links, attachments).
- Per-user access with each member's own personal access token or login, so project
  permissions and issue security apply per person.
- An admin-set project scope that no query, search or read can leave.
- Few API calls per question (section 5), with hard caps instead of unbounded fan-out.

Non-goals (follow-ups, section 10)
- Writing to Jira (creating or editing issues) — the MCP preset covers Cloud writes.
- Jira Cloud as a native type, Jira Service Management, Assets.

## 3. Shape

`jira_dc` is a **hybrid** connector, like ServiceNow in this codebase:

| Capability | What backs it |
|---|---|
| `QUERY` | `execute_query(spec)` → a paged `/rest/api/2/search` → `pandas.DataFrame` |
| `SEARCH_FILES` | Jira text search (`text ~ "…"`) inside the scope |
| `LIST_FILES` | recent issues, all projects in scope or one project key |
| `READ_FILE` | one issue as markdown-like text |

It shares its HTTP layer with the Confluence Data Center connector
(`_atlassian_dc_http.py`): URL and context-path validation, personal access token
(Bearer) or Basic auth, one egress-pinned session per thread, a 16 MB response cap,
never following redirects, 429 retry honouring `Retry-After`, and fixed error
messages for CAPTCHA, SSO redirects, proxy login pages, TLS and timeouts.

## 4. Query model

JQL selects *which* issues; it has no SELECT and no GROUP BY. So a query is a JSON
spec, and the agent aggregates the returned rows in pandas:

```json
{"table": "issues",
 "jql": "project = OPS AND status != Done AND created >= -90d ORDER BY priority DESC",
 "fields": ["key", "summary", "status", "priority", "assignee", "created"],
 "limit": 500}
```

| Spec key | Applies to | Meaning |
|---|---|---|
| `table` | all | one of the tables below (required) |
| `jql` | issues and the issue child tables | which issues; child tables return one row per child record of those issues |
| `fields` | all | columns to return (validated against the table) |
| `limit` | all | rows; default 1000, capped by the connection's Max rows |
| `project` | projects, versions, components, boards | optional project key |
| `board_id` | sprints | optional board id |

### Tables

| Table | Rows | Source |
|---|---|---|
| `issues` | one per issue; standard fields flattened, every custom field as a column (`story_points`, …), latest `sprint`, `epic_link`, `web_url` | `/search` |
| `issue_changelog` | one per field change (`field`, `from_value`, `to_value`, `author`, `created`) — cycle time, time in status | `/search?expand=changelog` |
| `issue_comments` | one per comment | embedded `comment`, rest from `/issue/{key}/comment` |
| `worklogs` | one per worklog (`time_spent_seconds`, `hours`) | embedded `worklog`, rest from `/issue/{key}/worklog` |
| `issue_links` | one per link (`relation`, `linked_issue_key`, `linked_status`) | embedded `issuelinks` |
| `attachments` | one per attachment (metadata) | embedded `attachment` |
| `projects` | one per project | `/project` (cached) |
| `versions`, `components` | per project | `/project/{key}/versions`, `/components` |
| `boards`, `sprints` | agile boards; sprints of scrum boards | `/rest/agile/1.0/board`, `/board/{id}/sprint` |
| `statuses`, `priorities`, `resolutions`, `issue_types` | lookups | cached |
| `fields` | field id ↔ name ↔ column | `/field` (cached) |

A connection can expose a subset (`tables` setting). A `users` table is deliberately
absent: Data Center's user search needs a search term, so "list every user" is not
a query Jira answers; people appear as columns on every table instead.

## 5. Call budget — not one call per table

Discovery: the table list is fixed, so `get_schemas` is **one** `/field` call
(custom fields become `issues` columns), cached for ten minutes per identity.

Per query, only the named table is fetched:

| Question | Calls |
|---|---|
| issues matching a JQL (N rows) | ⌈N / page⌉ searches (page up to 1000; Jira may clamp it — the client advances by the rows actually returned) |
| + links, attachments, sprint, versions, components on the issue | **+0** — same response; only the needed `fields=` are requested |
| change history | **+0** via `expand=changelog` on the same search; +1 only per issue whose history Jira cut short |
| comments / worklogs | **+0** from the copies embedded in each issue; +1 only per issue whose `total` exceeds what Jira embedded (it embeds 20) |
| lookups, projects, fields | 1 each, then cached for ten minutes |
| versions / components | 1 per project in scope |
| sprints | 1 per scrum board in scope |

Hard caps: at most 100 per-issue follow-ups per query — beyond that the query stops
with a fixed "narrow the JQL" error rather than returning partial data; at most 60
project/board calls per query; Data Center's 10,000-result search window is
reported, not silently truncated; the row cap is the connection's Max rows.

## 6. Scope and permissions

- **Identity**: every call runs as the connection's account or, on a
  `user_required` connection, as the member's own token or login (checked once on
  Save with `GET /rest/api/2/myself`; a refusal is a typed 422:
  `connection.jira_login_rejected` / `connection.jira_login_captcha`).
- **Project scope** (optional `projects`): two independent layers.
  1. The model's JQL must have balanced quotes and parentheses (checked before
     any request) and is sent as `project in (…) AND (<jql>) ORDER BY …`. A
     balanced string cannot close the wrapper, so it cannot widen the scope.
  2. Every returned issue's project key is re-checked before it becomes a row or a
     document. Reads of an issue outside the scope, and project/board/sprint
     parameters outside it, are refused.
- Issue security levels and project permissions are Jira's: a hidden issue is
  absent from search and a 404 on read.

## 7. Documents

`read_file("issue:OPS-123")` (also `OPS-123` or a `/browse/OPS-123` link on the same
host) returns: a header (project, type, status, priority, resolution, assignee,
reporter, dates, labels, components, fix versions, sprint, epic, link), the
description (rendered HTML converted to text with tables kept as markdown tables,
via the Confluence HTML converter), every comment, links and attachment metadata.

## 8. Configuration

| Setting | Default | Notes |
|---|---|---|
| Jira URL | — | include the context path, e.g. `https://intranet.example.com/jira` |
| Projects | all visible | hard scope |
| Tables | all | subset to expose |
| Max rows | 2000 | 1–10000 per query |
| Read issues as documents | on | off = `QUERY` only |
| Allow HTTP | off | isolated labs only |

Credentials: personal access token (Data Center 8.14+) or username and password,
each usable as the system identity or per user.

## 9. Testing

- `tools/jira/mock_jira_server.py`: a stdlib mock of the REST v2 and agile 1.0
  shapes, with a JQL engine (AND/OR/NOT, parentheses, IN, `~`, relative dates,
  ORDER BY, `currentUser()`, `openSprints()`), per-user projects, issue
  security, silent page clamping, embedded collections cut at 20, and CAPTCHA,
  SSO, HTML-page, disabled-token and 429 switches.
- `backend/tests/unit/test_jira_dc_client.py`: the contract, call counts included.
- `backend/tests/e2e/test_jira_dc.py`: connection API, catalog indexing, member
  credentials, per-member identity, agent tools.
- `docs/feedback-loops/jira-dc-connector.md`: the runnable loop, including a live
  run with Claude Haiku 5.5.

## 10. Follow-ups

- Jira Cloud as a native type: Cloud search moved to `/rest/api/3/search/jql`
  (token paging, no total), with bulk issue and changelog fetch endpoints.
- A bulk worklog feed (`/worklog/updated` + `/worklog/list`) for org-wide time
  reports over many issues.
- Jira Service Management (requests, SLAs) and Assets tables.
- A KMS-style diagnostics panel; outbound proxy support.
