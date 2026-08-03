# Feedback Loop — Power BI semantic model metadata (relationships, hidden keys, measures, types)

A semantic model carries four things a connector must recover: which columns
exist, what type they are, which measures encode the business logic, and how
the tables relate. The Power BI connector recovered exactly one of them —
column names — for every deployment without tenant-admin scope.

The visible symptom was an agent telling users that a fact table had no field
identifying an entity and could not be joined to its dimension, on a model where
that relationship was active and the key column was simply marked hidden.

## What was actually missing

| Metadata | Before | Source |
|---|---|---|
| Column names | ✅ | `COLUMNSTATISTICS()` |
| Data types | ❌ every column `"unknown"` | not returned by COLUMNSTATISTICS |
| Hidden columns | ❌ dropped by the scan parser | `isHidden` filter |
| Measures | ❌ hardcoded `[]` | admin scan only |
| Relationships | ❌ hardcoded `[]` | admin scan only |

The admin scan needs tenant-admin scope, which a delegated (OBO) identity never
holds and a service principal holds only with two Fabric admin-portal settings
enabled. So in a normal deployment the agent wrote DAX against an untyped,
join-less, measure-less schema.

Hiding a surrogate key once a relationship covers the join is standard
modelling practice — `isHidden` is a report-authoring flag, not a permission,
and the column stays fully queryable in DAX. Dropping hidden columns therefore
removed precisely the columns needed to join.

## What the tenant answers

Measured live (2026-08-02), not inferred.

| Probe | Result |
|---|---|
| `INFO.VIEW.COLUMNS() / .MEASURES() / .RELATIONSHIPS()` | **200** |
| bare `INFO.COLUMNS() / .MEASURES() / .RELATIONSHIPS()` | **400** |
| `COLUMNSTATISTICS()` | 200 — **hidden columns ARE returned** |
| all three UNIONed into one query | **200**, 48 rows |
| any of the above on an **RLS** model (SP identity) | **401** `PowerBINotAuthorizedException` |
| `POST /admin/workspaces/getInfo` | **401** |
| dataset listing `isEffectiveIdentityRequired` | **true** on RLS models, false elsewhere |

Six findings, each load-bearing:

1. **`INFO.VIEW.*` works on the JSON `executeQueries` endpoint.** Microsoft's
   docs say INFO functions are unsupported there — true for the *bare* family
   only. This is the whole fix: model metadata is readable with the querying
   identity's own token. No admin scope, no Premium capacity, no XMLA.
2. **All three can be UNIONed into a single request.** `executeQueries` accepts
   one query per call, so this is the difference between 1 and 3 round-trips per
   dataset. Discovery is rate-limited (~120 req/min/user, shared with the user's
   real queries), so on a tenant with thousands of models that is the difference
   between minutes and hours. Net cost is **unchanged** from the
   COLUMNSTATISTICS-only discovery it replaces.
3. **`COLUMNSTATISTICS` returns hidden columns**, so the missing key columns were
   never an API limitation — they were dropped by our own `isHidden` filter.
4. **`INFO.VIEW.*` is refused on an RLS model with exactly the same 401 as
   `COLUMNSTATISTICS`**, so replacing the older probe does not risk indexing a
   model the identity cannot actually query.
5. **`INFO.VIEW.MEASURES()` does not expose `[Expression]`.** Survivable: DAX
   invokes a measure by NAME, so the agent can use it without its definition.
   Expressions remain admin-scan-only.
6. **`isEffectiveIdentityRequired` is a delegated-available RLS flag** from the
   dataset listing we already fetch — no admin scope, no extra call.

## The fix

- `_get_model_metadata_via_dax()` — one UNIONed request per dataset returning
  columns (real types, hidden flags, data categories), measures (with return
  type), and relationships. Primary path; COLUMNSTATISTICS remains the fallback
  for endpoints that reject INFO functions.
- Internal `RowNumber-<GUID>` columns are now identified by the model's own
  `DataCategory`, with the name regex kept as a backstop.
- Inactive relationships are dropped — the engine ignores them without
  `USERELATIONSHIP`, so presenting them as joinable invites silently wrong joins.
- Hidden columns are kept and flagged `[hidden]`; any column a relationship
  joins on is re-added if discovery missed it.
- Failure is bounded: the first non-401/403/404 rejection disables further
  attempts, capping an unsupported endpoint at one wasted request per crawl.
  401/403/404 stays per-dataset so one RLS model cannot cost the tenant its
  metadata.
- **Column descriptors now survive persistence.** Three copies of
  `normalize_columns` reduced every column to `{name, dtype}` on write, silently
  discarding the role/hidden/returns metadata the renderer reads. Replaced by a
  single `normalize_indexed_columns`.
- **Per-user (overlay) schema is enriched from the canonical row.** The overlay
  decides WHICH columns a user sees; the canonical row describes WHAT they are.
  Only structural keys cross (`role`, `kind`, `hidden`, `is_partition`,
  `relationship_key`, `returns`) — free text such as measure expressions can
  name tables the user cannot see, and the canonical row was indexed by a
  broader identity.
- **Relationships are filtered to tables the user can see**, and `configuredBy`
  (owner email) / `reports[]` are withheld from per-user schema.
- **RLS is surfaced, not inferred**: `rowLevelSecurity` in table metadata from
  `isEffectiveIdentityRequired`, and `RLSNotAuthorizedForModel` vs
  `PowerBIEntityNotFound` are reported distinctly instead of collapsing into one
  "not authorized" string — "join an RLS role" and "get Build permission" are
  requests to different people.
- Prompt: distinguishes "not readable" from "does not exist"; instructs the
  agent to invoke measures by name rather than re-deriving them; and states that
  RLS *filtering* is invisible (HTTP 200, fewer rows) so totals from a
  row-secured model must never be described as organization-wide.

## Fixtures

`tools/agent/pbi_build_fixture_models.py` provisions two models:

- **FleetOps** — two facts over five dimensions, two conformed (both facts join
  `dim_vehicle` and `dim_date`), every foreign key hidden, a role-playing date
  materialised as a duplicate dimension, six measures.
- **SupplyChainDepth** — snowflake, `fact_shipment → dim_product → dim_category
  → dim_department`, so a department-level question requires traversing two
  relationships not present on the fact table.

Two API limits shaped them: `isActive: false` is rejected (HTTP 400) and a
second relationship to an already-related table is rejected as an ambiguous path
(Desktop demotes it to inactive instead), so inactive relationships cannot be
provisioned here — that path is covered by unit tests. RLS roles are not
creatable through any REST API (service-layer or XMLA, and XMLA write needs
dedicated capacity), so RLS models must be provisioned separately.

## Verification

Live, end to end through the product UI with real LLM completions:

| Question | Requires | Result |
|---|---|---|
| parts cost per depot | join across a **hidden** FK | **Northgate 2450, Southfield 1800** ✅ |
| freight cost by department | **3-hop** snowflake traversal | **Ambient 870, Chilled 640** ✅ |

Indexed schema after the fix (previously: 2 columns, no types, no measures, no
relationships):

```
fact_service_event
    svc_vehicle_key Integer [hidden] ... svc_completed_date_key Integer [hidden]
    labour_hours Number, parts_cost Number
    Total Parts Cost [measure -> Number] ... Avg Parts Cost per Event [measure -> Number]
    foreign key (svc_vehicle_key) references FleetOps/dim_vehicle(vehicle_key)
    ... 5 relationships total
```

## Regression notes

- The `INFO.VIEW.` prefix is load-bearing and pinned by a test; swapping in the
  bare form silently reverts Power BI to a names-only schema.
- Incremental discovery reuses stored definitions, so models indexed before this
  change keep their empty metadata until a reindex that does **not** pass
  `prior_tables`. A rollout mechanism for that is still outstanding.
- A control run with metadata stripped but the corrected prompt still produced
  the right answer — the DAX engine applies relationships whether or not we
  indexed them. The metadata fix and the prompt fix are therefore independently
  valuable: metadata makes the agent *correct and efficient*, the prompt makes it
  *willing to try*.
