# Feedback Loop — "We should add Jira — on-prem too — and not one call per table"

Jira was reachable only through the Cloud-only "Jira / Atlassian" MCP preset, so a
self-hosted Jira could not be connected, and even on Cloud nothing came back as
tables. The claim validated here: a native `jira_dc` connector that answers
analytic questions (counts, cycle time, hours) and reads single issues live, as
each identity, inside an admin-set project scope, with few API calls per question.

## Root cause (validated)

- No Jira client existed: `rg -i jira backend/app/data_sources/clients` matched
  nothing before this change. The only Jira path is the MCP preset
  (`backend/app/schemas/data_source_registry.py`, `"atlassian"`), pointed at
  `mcp.atlassian.com`.
- Searching "jira" in Add Connection on the base branch shows only the MCP tile
  (screenshot `media/pr/claude-optimistic-feynman-fa1wha/jira-before-search.png`).

## Loop A — deterministic (no external services)

Boundary: `tools/jira/mock_jira_server.py`, a stdlib server with the REST v2 and
agile 1.0 shapes under `/jira`, a JQL engine, per-user projects, issue security
(OPS-7), embedded comments and worklogs cut off at 20 (OPS-1 has 25 comments, OPS-2
23 worklogs), a silent page clamp and the on-prem failure switches.

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_jira_dc_client.py -q                        # 55 passed
uv run pytest tests/e2e/test_jira_dc.py -m e2e --db=sqlite -q             # 7 passed
uv run pytest tests/unit/test_connector_egress.py tests/unit/test_egress_package_b.py \
  tests/unit/test_query_params.py -k "jira or every_connector or literal_rule" -q
```

| Claim | Test |
|---|---|
| An issues query is **one** search call requesting only the needed `fields=` | `test_an_issue_query_is_one_search_call_requesting_only_the_needed_fields` |
| Changelog rides the same search; only a cut-off history costs a call | `test_changelog_comes_from_the_same_search`, `test_a_cut_off_changelog_costs_one_call_per_affected_issue_only` |
| Comments / worklogs: extra calls only for issues Jira cut off | `test_comments_are_fetched_only_for_issues_jira_cut_off`, `test_worklogs_are_fetched_only_for_issues_jira_cut_off` |
| Links and attachments cost 0 extra calls | `test_links_and_attachments_cost_no_extra_calls` |
| Discovery is one call for 16 tables; lookups cached | `test_schema_discovery_is_one_call_for_any_number_of_tables`, `test_lookups_and_fields_are_cached_per_identity` |
| Paging survives a silent clamp | `test_paging_survives_a_silent_page_clamp` |
| Unbounded fan-out stops with a fixed error, not partial data | `test_too_many_follow_ups_stop_with_a_fixed_error_not_partial_data` |
| Malformed JQL refused before any request; balanced JQL cannot leave the scope; rows re-checked | `test_unbalanced_jql_is_refused_before_any_request`, `test_balanced_jql_cannot_reach_outside_the_scope`, `test_rows_from_outside_the_scope_are_dropped_even_if_jira_returns_them` |
| Identity and issue security per member | `test_issues_run_as_the_identity_and_hide_secured_issues`, e2e `test_members_read_issues_as_themselves` |
| Member login refused on Save with typed codes | e2e `test_a_login_jira_rejects_is_refused_and_nothing_is_stored`, `test_a_captcha_locked_account_is_refused_with_its_own_code` |
| Catalog indexes 16 tables + custom fields | e2e `test_the_catalog_lists_the_jira_tables_with_custom_fields` |
| Agent tools: query + read inside the scope | e2e `test_agent_queries_tables_and_reads_an_issue_inside_the_scope` |

Mutation check (each mutation reverted afterwards):

| Mutation | Result |
|---|---|
| both scope layers removed | 6 failed |
| JQL shape check removed | 3 failed |
| comment follow-up removed | 2 failed |
| member login check removed | 3 failed |
| read-time scope check removed | 1 failed |

## Loop B — live UI with Claude Haiku 5.5

```bash
tools/agent/boot_stack.sh --dev && (cd backend && uv run python ../tools/agent/seed_org.py)
export BOW_ENCRYPTION_KEY=<a stable key>          # so backend restarts keep stored credentials readable
ANTHROPIC_API_KEY=... HAIKU_MODEL_ID=<Haiku 5.5 id from the model catalog> uv run python ../tools/agent/setup_haiku_llm.py
python3 tools/jira/mock_jira_server.py --port 8098                  # http://127.0.0.1:8098/jira
```

1. Add Connection → search "jira": the **Jira Data Center (on-prem)** tile next to the MCP tile.
2. Form: URL `http://127.0.0.1:8098/jira`, Projects `OPS, HR`, Allow HTTP, service PAT.
   Connect → "Connection saved · Discovered 16 tables".
3. Select tables: 16/16 active.
4. Ask: *"in project OPS, how many open issues does each assignee have, and what was the
   average cycle time (first In Progress to Done, in days) of OPS issues resolved in the
   last 40 days? Then summarize OPS-6 and what it is linked to."*

Haiku 5.5 answered in 34 s from `issues`, `issue_changelog` and `issue_links`.
Checked against the mock's data as the service account sees it:

| Value | Agent | Ground truth |
|---|---|---|
| Open issues per assignee | Eli 2, Alice 1, Bob 1, Unassigned 1 | same |
| Average cycle time | 10.2 days | 10.2 days |
| OPS-6 links | relates to OPS-1 (Done), is duplicated by OPS-11 (To Do) | same |
| Median / row count | 9.7 days over "8 rows" | 9.2 days over 7 issues — the model's own arithmetic slip; the rows the connector returned were correct |

Screenshots: `media/pr/claude-optimistic-feynman-fa1wha/jira-*.png`.

## Regression notes

- 2349 neighbouring tests pass (KMS, Confluence, file tools, schema context, egress,
  query params, license, connection and data-source e2e).
- `test_discovery_progress_contract.py::test_every_registered_catalog_can_cancel_before_external_io`
  fails locally only because this sandbox lacks `libodbc` (an unrelated connector
  cannot import); `jira_dc` and `confluence_dc` were checked directly and cancel
  before any I/O. CI has ODBC.
