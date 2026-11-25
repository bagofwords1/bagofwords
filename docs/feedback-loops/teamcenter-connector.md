# Feedback loop: Siemens Teamcenter connector — sandbox pass (mock Teamcenter + Playwright + Claude Haiku 5.5)

Date: 2026-11-25 · Branch: `ai/lucid-noether-6tkifa`
Evidence: `media/pr/teamcenter-connector/` · Design: `docs/design/teamcenter-connector.md`

New connector type `teamcenter`: business objects (one table per configured type, with the site's
custom properties), BOM, where-used and engineering-change links as tables, plus the documents attached
to items as files — read-only and live, as the member's Teamcenter identity, with sign-in by Teamcenter
login, Kerberos, or **Teamcenter SSO** (Entra ID / Active Directory behind Teamcenter Security Services).

## Why a stand-in, and what it does NOT prove

There is no public Teamcenter instance or container image (`nz4ypt/docker-tc` needs licensed Siemens
media) and Siemens' services reference sits behind the support portal. So this loop runs against
`tools/teamcenter/mock_teamcenter_server.py`: web tier (SOA JSON), TcSS login service and file server,
with shapes assembled from public captures (the Krusty84 Postman collection, tcsoapy, tc-mcp, the
Teamcenter 10.1 services reference and the Mendix connector docs). Its docstring lists which shapes are
captured and which are inferred.

Not proven here — needs **Loop B** against a real (pilot) Teamcenter:
- the inferred request/response fields of describeSavedQueries, executeSavedQueries,
  getTypeDescriptions2, getFileReadTickets and whereUsed, and the FMS download URL;
- the parameter names TcSS uses when it returns the browser (the client auto-detects any parameter
  whose name contains "token"/"user" and both are configurable), and Kerberos/IWA on the login service.
"Test connection" checks object types, saved queries, revision rule and documents separately, so the
first real install names exactly which of these differs.

## Loop A — deterministic (no external services)

```bash
cd backend
uv run pytest tests/unit/test_teamcenter_client.py tests/unit/test_teamcenter_schema_context.py -q
uv run pytest tests/e2e/test_teamcenter.py -m e2e --db=sqlite -q
```

Observed: `63 passed` (client contract incl. a "legacy" server with only the older service versions,
session reuse/expiry, access rules, BOM revision rules and window closing, where-used, change links,
documents, TcSS token / Kerberos sign-in) and `7 passed` (connection API, catalog indexing, member
logins refused with `connection.teamcenter_login_rejected`, the full TcSS browser sign-in through
`/api/connections/teamcenter/sso/callback`, stray/forged/empty returns refused, agent tools).

## Loop B-lite — live UI with a real LLM (Claude Haiku 5.5), mock Teamcenter

```bash
tools/agent/boot_stack.sh --dev
cd backend && uv run python ../tools/agent/seed_org.py
ANTHROPIC_API_KEY=… HAIKU_MODEL_ID=claude-haiku-5-5 uv run python ../tools/agent/setup_haiku_llm.py
python ../tools/teamcenter/mock_teamcenter_server.py --port 8099 \
  --app-root "BOW=http://0.0.0.0:3000/api/connections/teamcenter/sso/callback"   # base_url of the dev config
cd ../frontend && cp ../tools/agent/teamcenter_ui_flow.mjs .tc_flow.mjs
node .tc_flow.mjs connect ../media/pr/teamcenter-connector
node .tc_flow.mjs chat ../media/pr/teamcenter-connector "<prompt>" <tag>
BOW_ORIGIN=http://0.0.0.0:3000 node .tc_flow.mjs sso ../media/pr/teamcenter-connector <sso connection id>
```

| Step | Observed |
|---|---|
| Catalog search "plm" | Siemens Teamcenter tile with the new icon (`01`) |
| Connect form | setup steps incl. the TcSS callback URL to register; tuning fields folded under "Advanced" (`02`, `03a`) |
| Connect over plain HTTP without "Allow HTTP" | fixed message "HTTPS is required. Enable Allow HTTP only for an isolated test lab." |
| Connect (Allow HTTP on) | Connection saved · Discovered 9 tables (`04`); tables step lists the 5 type tables + bom, where_used, change_items, revision_rules (`05`) |
| "Which parts at any level of the BOM of PRT-1000 rev B are not released yet?" | PRT-1001/B, PRT-1004/A (level 1) and PRT-1003/A (level 2) — correct (`10-bom-final`) |
| "Max operating pressure and design code in the PRT-1000 spec? Open ECNs affecting PRT-1001?" | 16 bar, PUMP-ALPHA-77 read from the PDF; ECN-0001 (Executing, Open) impacting rev A, solution rev B — correct (`10-doc-final`) |
| SSO: Connect → TcSS login page → sign in as jdoe → back to BOW (`21b`, `22`) | "OAuth credentials saved" after a passing live test; `test-my-credentials` → "Connected to Teamcenter at 127.0.0.1 as Jane Doe (jdoe)" |

## What the live loop caught (and the fixes)

1. **BOM quantities missing** — line properties were read after the BOM window closed; lines die with
   their window. Now read while it is open (unit test `test_the_bom_follows_the_revision_rule…`).
2. **Document search too literal** — Haiku searched "PRT-1000 specification maximum operating pressure
   design code" and got nothing (all words had to be in the name, in order). Search now tries the
   phrase, each distinctive word, and the documents attached to any item ID in the question, ranked by
   matches (`test_a_question_finds_its_document_first`).
3. **Planner unaware of documents** — the connector's guide reaches the code writer only; the planner saw
   just the tables and answered "this source has no specification documents". A Teamcenter connection
   now also renders a `kind="documents"` descriptor (search/list/read guidance, "find and read the
   document BEFORE concluding the answer is unavailable") without removing its tables
   (`tests/unit/test_teamcenter_schema_context.py`). Jira and ServiceNow have the same blind spot today.
4. **Ambiguous change columns** — Haiku called ECN-0001 "TCM Released" (the linked part's status).
   `change_items` now has `change_maturity`/`change_closure` and `item_*` columns, and the relation tables
   carry column notes ("empty = not released (work in progress)").

## Regression notes

- `tests/unit/test_fast_sql_dialect.py`, `tests/e2e/test_connection_oauth_flow.py::TestUserOverlaySync` and
  `tests/e2e/test_oauth_connection_recovery.py` fail identically with this change stashed: this sandbox
  lacks `libodbc.so.2` (the Fabric client import), not a regression.
- Running pytest inside `backend/` while the dev backend runs writes `.pyc` files under `app/` and can
  wedge uvicorn's reloader; use `PYTHONDONTWRITEBYTECODE=1` (and pin `BOW_ENCRYPTION_KEY` so a restart
  keeps stored credentials readable).
