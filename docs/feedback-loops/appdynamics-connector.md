# Feedback loop: AppDynamics connector — full sandbox pass (real LLM + Playwright)

Date: 2026-08-17 · Branch: `claude/appdynamics-integration-research-fyhb0u`
Design doc: `docs/design/appdynamics-connector.md` · Evidence: `media/pr/appdynamics-connector/`

## What was verified, end to end

Stack: backend (sqlite, port 8000) + frontend (port 3000) + **mock AppDynamics
controller** (`tools/appdynamics/mock_controller.py`, port 8090, 21.x REST
shapes, seeded 2-app bank estate) + **real Anthropic completions** (provider
configured in `/settings/models` with `$ANTHROPIC_KEY`, Claude 4.5 Haiku).

1. **Connect form (schema-generated)** — matches the agreed UI contract:
   Controller URL, Account Name (`customer1`), auth dropdown defaulting to
   "Username + Password", username hint about automatic account-name append,
   masked password, Verify SSL toggle. `Test connection` →
   **"Connected successfully. Found 10 tables."**
   (`connect-form-test-ok.png`)
2. **Schema discovery** — "Discovered 10 tables · 0s"; all ten virtual tables
   (applications, tiers, nodes, backends, business_transactions,
   service_flows, metric_data, events, health_rule_violations, snapshots)
   land in `connection_tables`; activated via the agent Tables tab
   (`datasource_tables.is_active=1` for all 10).
   (`schema-discovery-10-tables.png`)
3. **Service-map prompt (real LLM)** — "Build the full service map … analyze
   the network structure":
   - agent queried `applications`/`tiers`/`backends`/`service_flows`
     (`Created Data service_flows 9.6s` visible in the run log),
   - produced the **11-edge** edge list (exactly the seeded estate),
     degree-in/out hub analysis, Oracle single-point-of-failure impact
     (api-services + payments both JDBC→retailbank-oracle-db), and a
     4-chart dashboard document "AppDynamics Service Map & Network Analysis".
   (`service-map-analysis.png`, `dashboard-service-inventory.png`,
   `service-flows-degree-analysis.png`)
4. **Metrics prompt (real LLM)** — ranked BT response times (payments
   `/transfer` correctly slowest per the seeded baselines), `/transfer`
   trend chart from `metric_data`, and open health-rule violations with
   severity chips (CRITICAL `/transfer`, WARNING `notifications`),
   correlated in the narrative. (`health-violations-findings.png`)
5. **Lower layers** —
   - `completions`: 2 user + 2 system, all `success`;
   - backend log: 99 `POST https://api.anthropic.com/v1/messages` calls, 200 OK;
   - mock controller log: the full REST spread (applications ×41, metrics
     browse ×50, tiers ×22, snapshots/violations/events, one `metric-data`
     series fetch for the trend chart) — every call carried `output=JSON`
     and account-qualified Basic auth.

## Live-controller validation (SaaS trial, v26.7.0)

A free-trial SaaS controller was seeded from this sandbox: three Flask tiers
instrumented with the official Python agent 26.7.1 (`pyagent run`, env-var
config, routed through the egress proxy via
`APPDYNAMICS_CONTROLLER_HTTP_PROXY_HOST/PORT`), app `bow-sample-bank`,
~45 min of synthetic traffic. Results, all with the UNMODIFIED client:

- Basic auth with the account access key works as `user@account`; a bare
  username is a 401 on the real controller (the @-append rule is mandatory).
- Default output is XML on the real controller — `output=JSON` required.
- `test_connection`, the 10-table catalog + enrichment, `service_flows`
  (real cross-tier edges from External Calls names), `business_transactions`,
  and `metric_data` rollups all worked live (e.g. `/transfer` ≈ 413ms avg vs
  `/login` ≈ 21ms — matching the app's built-in latency profile).
- **Fixture reconciliation caught one real bug**: event type `DEPLOYMENT`
  (from docs) is a 400 on the real API — the valid type is
  `APPLICATION_DEPLOYMENT`. `DEFAULT_EVENT_TYPES` and the simulator fixed.
- Real response bodies captured into `backend/tests/unit/fixtures/appdynamics/`
  (see its README for provenance + findings).

Agent-seeding gotchas (if re-seeding a controller from a sandbox):
- Multiple `pyagent` processes MUST have distinct `APPDYNAMICS_AGENT_BASE_DIR`s
  — a shared `/tmp/appd` makes simultaneous agents race on node identity and
  every tier collapses into one. Stagger startups.
- Stale metric-tree branches from a mislabeled run persist for the retention
  window (they appear as extra service_flows edges — harmless, but explains
  "impossible" edges like a tier calling itself).
- Kill agent processes via /proc cmdline scan, not `pkill -f` (the pattern
  matches your own shell and kills it; a surviving proxy keeps the stale
  identity alive).

## Gotchas found while driving the UI (for the next agent)

- The report page can have TWO `[contenteditable]`s (chat composer + open
  dashboard-document editor). `.last()` types into the DOC — use `.first()`
  (or pick by bounding box x < 620) for the chat composer, and pick the send
  button the same way.
- Mention the data agent with type-ahead: `@AppD` + Enter (clicking the
  mention menu's "Agents" row by text collides with the left-nav "Agents").
- The Add Connection modal AUTO-OPENS on `/agents/new` when no connection
  exists — don't click "Create new connection" first.
- Connect-form fields have stable ids derived from the Pydantic field names
  (`#controller_url`, `#account_name`, `#username`, `#password`).
- Sign-up rejects reserved TLDs (`.test`) — use a real-looking domain.
- First visit to heavy pages cold-compiles in Nuxt dev — wait 8-15s, not 2s.

## Re-run

```bash
# mock controller
cd tools/appdynamics && uv run --project ../../backend python mock_controller.py &
# creds: admin / Secret123! (account customer1), OAuth bow_api / OauthSecret456!,
# empty-world: emptyuser / Empty123!
cd backend && BOW_DATABASE_URL='sqlite:///db/app.db' uv run --extra dev \
  pytest tests/unit/test_appdynamics_client.py -q     # 24 tests
```
