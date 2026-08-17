# AppDynamics fixtures — captured from a REAL controller

Captured 2026-08-17 from a live **SaaS trial controller v26.7.0**
(`bow-sample-bank`: three Python-agent-instrumented Flask tiers —
web-portal → api-services → payments — driven with synthetic traffic).
These are genuine Controller REST API response bodies (no credentials in them);
treat them as the shape-of-truth when the simulator
(`tools/appdynamics/mock_controller.py`) and the docs disagree.

| File | Endpoint |
|---|---|
| applications.json | `/controller/rest/applications` |
| tiers.json / nodes.json / backends.json | per-app entity lists |
| business_transactions.json | `/business-transactions` |
| metrics_root.json / metrics_oap_tiers.json | metric-browse (root, Overall Application Performance) |
| metrics_external_calls.json | External Calls branches per tier (service-map source) |
| metric_data_bt_art_rollup.json | `metric-data` BT avg-response-time, rollup=true |
| events.json | `/events` (see note) |
| healthrule_violations.json / snapshots.json | violations (empty — none fired), request snapshots |

Reconciliation findings (doc/simulator vs real 26.7):
- **`DEPLOYMENT` is not a valid event type** — the API returns 400; the real
  type is `APPLICATION_DEPLOYMENT`. Client + simulator fixed accordingly.
- `snapshots` confirms the `errorOccured` single-r spelling and epoch-millis
  timestamps.
- Metric-browse entries carry `name` + `type` (`folder`/`leaf`) exactly as the
  simulator models them.
- The customer's target is on-prem **21.4**; these fixtures are 26.7 —
  field sets may include additive keys 21.4 lacks. Read defensively.
