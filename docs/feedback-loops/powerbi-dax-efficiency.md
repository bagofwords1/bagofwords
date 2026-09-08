# Feedback Loop — Power BI DAX efficiency (raw pull + pandas aggregation)

Customer report: a "sum of quantities by product in a date range" question on a
Power BI semantic model took ~90s; a hand-optimised DAX returned in ~15s. In one
case the agent pulled the whole population first and ran a second, aggregated
query afterwards.

Reproduced live (Loop B, real tenant + Claude Haiku 4.5, `SalesPush/Sales`,
300 rows) through the product UI with Playwright. Ground truth from a direct
`executeQueries` call: 8 products, 131 rows in Q1 2026, 630 units.

## Root cause

The Power BI DAX guide (`PowerBIClient.system_prompt`) covered syntax only —
nothing about where filtering and aggregation should happen. `executeQueries`
streams every returned row through the REST API as JSON, so the cost of a query
is the rows and columns it returns, not the engine's work.

Baseline generated code, prompt "total quantity sold by product between
Jan 1 and Mar 31 2026" (planner's interpreted_prompt DID ask for SUM by
product — the coder still could not express a filtered aggregate in DAX):

```dax
EVALUATE SUMMARIZECOLUMNS('Sales'[Product], "total_quantity", SUM('Sales'[Quantity]))   -- unfiltered, discarded
EVALUATE FILTER('Sales', 'Sales'[OrderDate] >= DATE(2026,1,1) && 'Sales'[OrderDate] <= DATE(2026,3,31))  -- every row+column
```
then `df.groupby('Product')['Quantity'].sum()` in pandas. Two round trips, one
full raw transfer. Second baseline ("quantity sold by product ...") first failed
with `The syntax for 'AND' is incorrect` (22.7s), then retried with the same raw
FILTER + pandas shape (14s); 60s end to end.

## Fix

1. `powerbi_client.py` — "Query Efficiency" section in the DAX guide: one query
   per result; filter INSIDE `SUMMARIZECOLUMNS` (filter-table argument) or
   `CALCULATETABLE`; row listings via `SELECTCOLUMNS` over `CALCULATETABLE`;
   `&&`/`||` not infix `AND`/`OR`; the slow shape shown and named. Every example
   was executed against the tenant before being written into the guide.
2. `coder.py` — the Power BI bullet now says to push filter and aggregation into
   the DAX and never `EVALUATE` a table then `groupby` in pandas.
3. `planner/prompt_builder.py` (legacy, `BOW_PLANNER=v2` only) — dropped the
   rule "'by X' → return granular rows and let the viz layer aggregate" and its
   "revenue by month → granular rows" example; grouped asks aggregate in the
   query, row asks return rows. The active v3 builder never carried the rule.

## After (same prompts, same model)

```dax
EVALUATE
SUMMARIZECOLUMNS(
    Sales[Product],
    FILTER(ALL(Sales[OrderDate]), Sales[OrderDate] >= DATE(2026, 1, 1) && Sales[OrderDate] <= DATE(2026, 3, 31)),
    "TotalQuantity", SUM(Sales[Quantity])
)
```
One query, 8 rows back, no pandas aggregation, 630 total — both phrasings. A
row-listing prompt ("list the individual orders for January 2026 ... I want the
rows") still returns rows: `SELECTCOLUMNS(CALCULATETABLE(Sales, <date range>), ...)`,
47 rows, only the four requested columns.

| run | DAX round trips | rows transferred | create_data |
|-----|-----------------|------------------|-------------|
| before, "total by product" | 2 | 300 + 131 (all columns) | 8.9s |
| before, "by product" | 2 (1 syntax error) | 131 (all columns) | 22.7s + 14.0s |
| after, "total by product" | 1 | 8 | 7.7s |
| after, "by product" | 1 | 8 | 10.9s |
| after, row listing | 1 | 47 (4 columns) | 6.8s |

The tenant is tiny, so the wall-clock gain here is small; on the customer's
model the raw transfer was the 90s. The shape change is what matters.

## Sandbox notes (not fixed here)

- A report started in Auto mode (no agent pinned) rendered an empty agents
  roster, and `context_hub._SCHEMA_CACHE` stored that empty schema under the
  same key a pinned run uses (org + agent ids). For the 300s TTL every run
  saw zero tables ("No active tables matched", `describe_tables` → 0).
  Restarting the backend cleared it. Pin the agent via the
  `agent-scope-trigger` picker before the first prompt.
- `refresh_warm` gathers four builders on one AsyncSession; the loser logs
  "This session is provisioning a new connection" from
  `query_context_builder` and returns []. Noise, not the failure above.
- The org setting `enable_load_step` defaults off, so a follow-up
  "now summarize by product" re-queries the source instead of reusing the
  previous step's rows in pandas — the customer's second complaint. Not
  changed in this loop.
