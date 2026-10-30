# Feedback Loop — Azure Cost Management connector

The Azure counterpart of the AWS Cost Explorer connector (`aws_cost`): a new
`azure_cost` connection type that lets the agent answer "what did we spend on
Azure, where, and where is the month heading" against one Azure scope.

Status: **built and verified offline; not yet verified against a live Azure
tenant.** Loop B below is what closes that gap — it needs credentials this
sandbox does not have.

## What was built

| Piece | Where |
|---|---|
| Client: Query / Forecast / Dimensions REST calls, dict query envelope → REST body, `nextLink` paging, 429 back-off on the Cost Management retry headers, `PreTaxCost` fallback | `backend/app/data_sources/clients/azure_cost_client.py` |
| Config (`scope`, default cost view) + credentials (service principal; managed identity / DefaultAzureCredential) | `backend/app/schemas/data_sources/configs.py` (`AzureCost*`) |
| Registry entry, explicit `client_path` | `backend/app/schemas/data_source_registry.py` (`azure_cost`) |
| Icon + picker keywords | `frontend/public/data_sources_icons/azure_cost.svg`, `DataSourceIcon.vue`, `useDataSourcePicker.ts` |
| Unit tests (44) | `backend/tests/unit/test_azure_cost_client.py` |
| Live checks | `backend/tests/integrations/ds_clients.py` (`azure_cost`), `backend/tests/integrations/verify_azure_cost.py` |

No new Python dependency: `azure-identity` and `requests` were already pinned.

### Tables the agent sees

| Table | Backed by | Columns |
|---|---|---|
| `costs` | `POST {scope}/providers/Microsoft.CostManagement/query` | `Cost`, `Currency`, `UsageDate` / `BillingMonth`, + every dimension the scope reports |
| `forecast` | `POST …/forecast` | `Cost`, `UsageDate`, `CostStatus`, `Currency` |
| `dimension_values` | `GET …/dimensions?$expand=properties/data` | `dimension`, `value` |

### Query envelope

```python
client.execute_query({"start": "-30d", "group_by": ["ServiceName"]})
client.execute_query({"start": "start_of_month", "granularity": "Daily",
                      "filter": {"ResourceGroupName": ["rg-prod"], "tag:env": ["prod"]}})
client.execute_query({"type": "AmortizedCost", "timeframe": "TheLastMonth", "group_by": ["ResourceId"]})
client.execute_query({"table": "forecast"})
```

### AWS Cost Explorer ↔ Azure mapping

| AWS Cost Explorer | Azure Cost Management |
|---|---|
| `get_cost_and_usage` | Query API (`/query`) |
| `get_cost_forecast` | Forecast API (`/forecast`) |
| `get_dimension_values` | Dimensions API (`/dimensions`) |
| `UnblendedCost` / `AmortizedCost` | `type: ActualCost` / `AmortizedCost` |
| `GroupBy` (max 2) | `grouping` (max 2, `Dimension` or `TagKey`) |
| `SERVICE`, `LINKED_ACCOUNT`, `REGION` | `ServiceName`, `SubscriptionId`, `ResourceLocation` |
| IAM `ce:GetCostAndUsage` | RBAC **Cost Management Reader** on the scope |
| ~$0.01 per request | free, but tenant quota: 12 QPU/10 s, 60/min, 600/h (1 QPU ≈ 1 month queried) |

## Loop A — deterministic (CI)

```bash
cd backend
TESTING=true ENVIRONMENT=production uv run pytest tests/unit/test_azure_cost_client.py --db=sqlite -q
TESTING=true ENVIRONMENT=production uv run pytest tests/e2e/test_data_source.py tests/e2e/test_connection.py --db=sqlite -q
```

| Check | Result |
|---|---|
| Scope normalization (GUID, path, ARM URL, billing scope) | pass |
| Envelope → body: default window, relative dates, named timeframes, grouping incl. tags, flat + native filters, single-clause filter not wrapped in `and`, limits (max 2 group-bys / metrics) rejected before any HTTP call | pass |
| Response → DataFrame, `UsageDate` parsed, `nextLink` followed with the same body, `limit` stops paging, 204 → empty frame | pass |
| 429: waits the longest Cost Management retry header, retries; >60 s wait fails at once with a quota message; bounded retries | pass |
| `Cost` rejected → retried as `PreTaxCost` and exposed as `Cost`; an explicitly chosen metric is not swapped | pass |
| Forecast window to month end; dimension values; discovery fallback when Dimensions API is denied | pass |
| `test_connection`: MTD spend message; 403 → names the Cost Management Reader role; token failure → no HTTP call | pass |
| Mutation check: breaking retry-header max, single-clause filter, or paging fails 3 tests | pass |
| Existing data-source / connection e2e suites | 54 passed |

## Loop B — live Azure (not yet run)

### What it needs

1. An Azure subscription with some spend (any account type; ideally run once
   on a pay-as-you-go/EA subscription **and** once on an MCA billing scope,
   since the cost column name is suspected to differ).
2. An Entra app registration (service principal) with a client secret.
3. Role assignment: **Cost Management Reader** on the subscription (or the
   billing reader role for a billing scope). EA only: "Account owners can view
   charges" must be on.
4. Optional: a tag key used on resources (`--tag-key`).

```bash
az ad sp create-for-rbac --name bow-cost-reader \
  --role "Cost Management Reader" --scopes /subscriptions/<subscription-id>
```

### Run

```bash
cd backend
AZURE_COST_SCOPE=/subscriptions/<id> AZURE_TENANT_ID=... AZURE_CLIENT_ID=... AZURE_CLIENT_SECRET=... \
  uv run python tests/integrations/verify_azure_cost.py --tag-key Environment --out azure_cost_verification.json
```

Then the UI pass (`tools/agent/boot_stack.sh`): create the connection → Test
connection → tables list shows `costs` / `forecast` / `dimension_values` →
prompt "What did we spend on Azure last month by service?".

### Open questions Loop B answers

| # | Question | Why it matters | Current handling |
|---|---|---|---|
| 1 | Does the Query API return `Cost` or `PreTaxCost` (or the alias) per account type? | Docs examples request `Cost`/`totalCost` but show `PreTaxCost` back | Retry with `PreTaxCost` on 400; rename the single cost column to `Cost` |
| 2 | Is the grouping name `ResourceGroupName` or `ResourceGroup`? | Docs use both | Prompt uses `ResourceGroupName`; discovery lists what the scope reports |
| 3 | Is a custom range > 12 months rejected? | Only third-party sources state a 1-year cap | Prompt says keep ≤ 12 months; no auto-split |
| 4 | Which throttling header is actually sent? | Docs name `…costmanagement-qpu-retry-after`; spec names `…consumption-retry-after` | Client reads all five + `Retry-After`, takes the max |
| 5 | Does the Dimensions API return data at this scope type? | Returns 204 for some account types | Falls back to a static dimension list |
| 6 | Does `test_connection` (~1 QPU) feel fast enough? | First query per scope can be slow | 120 s request timeout |

## Not in scope

- Exports / Cost Details (CSV reports) — for very large, unaggregated datasets.
- Budgets, Advisor recommendations, reservations utilization.
- Multiple scopes per connection (add one connection per scope for now).
