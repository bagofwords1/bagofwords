# Feedback Loop — "We have issues with Confluence; it should work like KMS, and on-prem"

Before this change, Bag of Words reached Confluence only through the
"Jira / Atlassian" MCP preset. That preset is the Cloud-only Atlassian Rovo
server (`mcp.atlassian.com`), so a self-hosted Confluence Data Center or
Server could not be connected at all. Searching the connector catalog for
"confluence" returned nothing. The claim validated here: a native
`confluence_dc` file connector that lets an agent search, browse and read an
on-prem Confluence live, as each identity, with the on-prem failure modes
named instead of failing silently.

## Root cause (validated)

* There was no Confluence client. `rg -i confluence backend/app` matched only
  the MCP tool names in `backend/app/schemas/data_source_registry.py`
  (`_TOOLS_ATLASSIAN`) and the preset's description.
* The preset hard-codes `server_url=ATLASSIAN_MCP_URL` (`https://mcp.atlassian.com/v1/mcp/authv2`)
  with `allowed_auth=["dcr"]`. Rovo MCP serves Cloud only.
* A Confluence DC URL pasted into a custom MCP connection still shows a green
  test: any 401 counts as "Server reachable — sign-in required"
  (`backend/app/services/connection_service.py`, `_looks_like_auth_challenge`).
  That path is left unchanged here; see the PR's follow-ups.

## Loop A — deterministic (no external services)

The boundary is `tools/confluence/mock_confluence_server.py`: a stdlib HTTP
server speaking the Data Center REST shapes under a `/confluence` context path.
It has three users (alice, bob, svc) with different space access, a page
restricted to alice and svc, a server-side `limit` cap, and switches for
CAPTCHA, SSO redirect, HTML login page, disabled PATs, 429 and oversized bodies.

```bash
cd backend
export TESTING=true BOW_DATABASE_URL="sqlite:///db/app.db"
uv run pytest tests/unit/test_confluence_dc_client.py tests/unit/test_confluence_html.py -q
uv run pytest tests/e2e/test_confluence_dc.py -m e2e --db=sqlite -q
uv run pytest tests/unit/test_connector_egress.py tests/unit/test_egress_package_b.py -k "confluence or every_connector" -q
```

Observed on this branch: **79 passed** (unit), **10 passed** (e2e), **15 passed** (egress).

What the suites pin (behaviour, not implementation):

| Claim | Test |
|---|---|
| Members read as themselves; restricted pages stay hidden per person | `test_user_required_members_read_as_themselves`, `test_search_runs_as_the_identity_and_hides_restricted_pages`, `test_a_restricted_page_reads_as_not_found` |
| A wrong PAT or password is refused on Save, nothing stored, secret never echoed | `test_a_login_confluence_rejects_is_refused_and_nothing_is_stored[userpass/token]` |
| CAPTCHA lock gets its own code; that member's PAT still saves | `test_a_captcha_locked_account_is_refused_with_its_own_code` |
| Space list is a hard scope (search, browse, read), even against CQL injection | `test_space_scope_limits_search_even_against_cql_injection`, `test_browsing_never_leaves_the_configured_spaces`, `test_reads_never_leave_the_configured_spaces`, e2e `test_agent_is_told_the_scope_then_searches_browses_and_reads_inside_it` |
| Context path, SSO redirect, proxy login page are named, never followed | `test_a_missing_context_path_is_named_as_not_the_rest_api`, `test_sso_and_proxy_pages_are_named_and_not_followed` |
| Paging past the server cap; 429 honoured then typed | `test_search_pages_past_the_server_cap`, `test_a_429_is_retried_after_retry_after`, `test_persistent_rate_limiting_is_a_typed_throttle_error` |
| Tables survive as markdown | `test_read_returns_a_header_and_tables_as_markdown`, `test_confluence_html.py` |
| Destination goes through egress pinning; metadata hosts are never contacted | `test_every_connector_on_requests_has_a_row_in_the_matrix`, `test_confluence_dc_base_url_never_contacts_metadata` |

Mutation check (rule 6 of `backend/tests/AGENTS.md`):

* Removing the read-time space check fails 2 tests.
* Removing the member login check on Save fails 3 tests.
* Removing only the CQL `space in (...)` clause, or only the CQL quoting,
  keeps the scope and injection tests green. That is by design: each has a
  second layer. Search results whose space is outside the scope are also
  dropped client-side, and CQL operators are stripped from the query before
  quoting.

## Loop B — live UI with a real LLM (Claude Haiku 5.5)

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
ANTHROPIC_API_KEY=... HAIKU_MODEL_ID=<Haiku 5.5 id from the model catalog> uv run python ../tools/agent/setup_haiku_llm.py
python3 ../tools/confluence/mock_confluence_server.py --port 8097      # http://127.0.0.1:8097/confluence
```

Then in the UI (driven with Playwright):

1. Agents → New → Add Connection, search "confluence". The **Confluence
   Data Center (on-prem)** tile appears; on the base branch: "No data sources
   found".
2. The schema-generated form: URL `http://127.0.0.1:8097/confluence`,
   Spaces `ENG, HR`, Allow HTTP on, the svc PAT. Connect succeeds.
3. Configure knowledge: the card shows `Live`, Scope chips `ENG` `HR`, and a
   live listing (ENG 7 pages, HR 2).
4. New report, asking: *"what is the RTO in the database failover runbook and
   who owns step 2? Also, how many vacation days does someone with 4 years of
   tenure get? Cite the pages."*

   Haiku 5.5 answered in 14 s: **RTO 15 minutes, step 2 owned by the DBA**
   (from the runbook's table), **23 days** (the 3-5 year band of the HR table),
   each with a link to the page and its last-update line.

Screenshots: `media/pr/claude-optimistic-feynman-fa1wha/`.

Gotcha hit while running Loop B: `tools/agent/restart_backend.sh` without a
pinned `BOW_ENCRYPTION_KEY` makes every stored credential (connections *and*
the LLM provider) undecryptable after the restart (`cryptography.fernet.InvalidToken`).
Export a stable key before booting if you plan to restart.

## Regression notes

* `tests/e2e/test_connection_file_browse.py::test_contract_sharepoint_server`
  fails with `KeyError: 'files'` with this change stashed too. It is
  pre-existing and unrelated.
* Neighbouring suites run green: KMS unit and e2e, license, credential-detail,
  data-source-kind, egress (1480 tests), and the frontend unit tests
  (`kmsMemberCredentials`, `agentFilesPanelLiveSources`, `useDataSourcePicker`,
  `useToolConnectionIcon`, `catalogNouns`).
