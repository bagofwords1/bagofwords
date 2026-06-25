# Sandbox Feedback Loop — Cost console (LLM spend by user / agent / group)

Builds and validates the new **Cost** tab under `/monitoring`: an admin view of
LLM token & cost spend, broken down by **user**, **agent (data source)**,
**group**, model, provider, or feature (scope), over a date range — analogous to
`/monitoring` (Explore) and `/monitoring/diagnosis`, but dedicated to cost.

This doc is the runnable feedback loop used to build and confirm the feature in a
fresh cloud sandbox, following `docs/design/sandbox-feedback-loop.md`.

---

## What was built

**Attribution (the missing half).** Before this change `llm_usage_records` stored
per-call tokens/cost but nothing linking a record to *who* ran it or *against
which* report/data source — so cost could only be grouped by model. Added:

1. **Schema** — `organization_id`, `user_id`, `report_id`, `data_source_id`
   (all nullable, indexed) on `llm_usage_records`
   (`backend/app/models/llm_usage_record.py` + migration
   `b1c2d3e4f5a6_add_attribution_to_llm_usage_records.py`).
2. **Ambient propagation** — `app/ai/llm/usage_attribution.py` holds a contextvar;
   `AgentV2.main_execution` stamps `{org, user, report, data_source}` once at run
   start; `LLM._schedule_usage_record` snapshots it *synchronously at schedule
   time* (so it survives the hop onto the background recorder task and the
   worker-thread judge), and `LLMUsageRecorderService.record` persists it.
   `organization_id` always falls back to the model's org, so even
   background/unattributed calls are org-scoped.

**API** — `GET /console/metrics/cost?group_by=<dim>` (`manage_settings`),
returns KPI totals, a dense daily timeseries, and a breakdown for
`group_by ∈ {model, provider, user, data_source, group, scope}`
(`console_service.get_cost_metrics` / `_cost_breakdown`).

- KPI totals + timeseries come from the un-expanded records, so they're always
  exact. `data_source` and `group` breakdowns *fan out* each record across every
  data source of its report / every group of its user, so per-row sums can
  exceed the headline total by design. NULL report/user → "Unattributed".
- Token totals are computed per-provider (Anthropic counts cache tokens; OpenAI
  folds them into prompt) and merged, so `sum(items.tokens) == KPI tokens` for
  every grouping.

**Frontend** — `frontend/pages/monitoring/cost.vue` + a "Cost" tab in
`frontend/layouts/monitoring.vue`; KPI cards, a cost/tokens trend chart, a
group-by selector, and a breakdown table with a share bar. i18n under
`locales/en.json` → `monitoring.cost.*` (other locales fall back to `en`).

---

## Environment setup (fresh sandbox)

Python 3.12 (the codebase uses 3.12 f-strings); the sandbox default may be 3.11.

```bash
cd backend
python3.12 -m venv .venv && source .venv/bin/activate
pip install uv && uv sync --frozen --extra dev
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD="dummy"
mkdir -p db uploads/files uploads/branding
alembic upgrade head        # applies the attribution migration (head: b1c2d3e4f5a6)
```

---

## Loop A — Attribution & grouping (pytest, no live LLM)

Self-contained: seeds one org with Alice (in group G1) + Bob (no group), two data
sources, two reports (R2 is multi-source), two models, and three usage records —
one for Alice/R1/DS1, one for Bob/R2/(DS1+DS2), one background record with no
user/report. Then asserts every grouping lands in the right bucket and that KPI
totals never double-count.

```bash
cd backend
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD="dummy"
TESTING=true pytest -s -m e2e --db=sqlite \
  tests/e2e/test_cost_metrics_attribution.py
```

**Observed (PASS):**

```
[model] {claude: 0.0115, gpt: 0.02}
[user] {Alice: 0.0105, Bob: 0.02, Unattributed: 0.001}
[group] {Analysts: 0.0105, Unattributed: 0.021}        # Bob has no group; bg record has no user
[data_source] {DS1: 0.0305, DS2: 0.02, Unattributed: 0.001}  # R2 fans out to DS1+DS2
[scope] planner/answer/data_source.summary buckets correct
1 passed
```

KPI totals are identical across every grouping (`total_calls == 3`,
`total_cost == 0.0315`) and the timeseries sums back to that total.

> Note: the HTTP-based tests in `tests/e2e/test_console_metrics.py` fail in this
> sandbox with `create_user -> 404` — **pre-existing and unrelated** (the signup
> route isn't mounted under the default dev config; documented in
> `sandbox-feedback-loop.md`). Loop A seeds the DB directly to avoid that.

---

## Loop B — Live end-to-end (real Anthropic key)

Confirms attribution is stamped by the *real* agent path (planner, tools, and the
worker-thread judge), not just hand-seeded rows.

```bash
cd backend && source .venv/bin/activate
export BOW_DATABASE_URL="sqlite:///db/app.db" BOW_SMTP_PASSWORD="dummy" \
  BOW_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')" \
  BOW_CONFIG_PATH="$PWD/../configs/bow-config.sandbox.yaml"   # local auth + signups
python main.py &        # backend, auto-reloads

# register sandbox@bow.dev, create Anthropic provider with ANTHROPIC_API_KEY
# (env only — never commit), install chinook demo, create a report, POST a
# completion ("How many tracks are in the database?"). See the curl recipes in
# docs/design/sandbox-feedback-loop.md.
```

**Observed (PASS):** one completion produced 6 usage records — scopes `planner`
(×3), `judge.instructions_context`, `create_data.code_gen`, `create_data.viz_infer`
— with **org / user / report / data_source set on 6/6** (tool and worker-thread
judge calls included). The endpoint then returns, for `group_by=user`, "Sandbox
Admin = $0.16"; for `group_by=data_source`, "Music Store = $0.16"; for
`group_by=scope`, the per-feature split; and `sum(items.tokens) == KPI tokens`
for every grouping.

---

## Loop C — Frontend (Playwright screenshot)

```bash
cd frontend && yarn install && yarn dev &     # HMR
npx playwright install chromium
# log in via /users/sign-in (sandbox config = local_only), navigate to
# /monitoring/cost, screenshot. See docs/design/sandbox-feedback-loop.md §
# "Authenticated UI Inspection".
```

Screenshots of the rendered Cost tab (KPI cards, trend chart, group-by breakdown)
are attached in the session.

---

## What this proves

- The agent stamps attribution on **every** LLM call of a run — including tool
  sub-calls and the worker-thread judge — so spend is attributable to user,
  report and data source going forward (older rows stay NULL → "Unattributed").
- The cost API groups spend by user / agent (data source) / group / model /
  provider / feature over time, with exact, non-double-counted KPI totals and a
  consistent token total across groupings.
