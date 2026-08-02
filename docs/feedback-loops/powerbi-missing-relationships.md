# Feedback Loop — Power BI semantic models indexed without relationships

A user asked whether a fact table could be joined to a dimension table. The
agent answered that there was **no direct relationship**, that the fact table
had **no field identifying the entity**, and therefore that no real join was
possible. All three statements were wrong: the model had an active many-to-one
relationship between exactly those tables, on exactly the column the agent said
did not exist.

## Two independent defects

**1. Relationships were only ever readable with tenant-admin scope.**
`_parse_admin_scan_tables` (Admin Scanner API) was the sole producer of
relationships. Every other introspection path returned `[]` by construction —
`_get_tables_via_column_stats_with_reason` even documented it ("No relationships
available via COLUMNSTATISTICS"). The scanner needs admin scope, which a
delegated (OBO) identity never holds and a service principal holds only when two
Fabric admin-portal settings are enabled. So in a normal deployment `fks` was
empty for every semantic model.

**2. The admin-scan parser dropped every `isHidden` column.** Hiding a
surrogate/foreign key once a relationship covers the join is standard modelling
practice, so this removed precisely the join keys. `isHidden` is a
report-authoring flag, not a permission — the column is fully queryable in DAX.

The two compounded: no relationship *and* no key column, so nothing in the
agent's context suggested the tables were connected.

**3. The prompt turned missing data into a confident denial.** The connector's
system prompt said "Relationships between tables are in `fks`", making empty
`fks` read as "this model has no relationships" rather than "we could not read
them". Combined with a reliability instruction not to invent joins, the agent
correctly refused — on a false premise.

## What the tenant actually answers

Measured live (2026-08-02) against a push dataset built to the failing shape:
`fact_meals[fk_entity]` → `dim_entity[primary_key]`, both key columns **hidden**,
relationship active, many-to-one.

| Probe | Result |
|---|---|
| `EVALUATE COLUMNSTATISTICS()` | **200** — hidden columns **ARE** returned |
| `EVALUATE INFO.VIEW.RELATIONSHIPS()` | **200** — full relationship metadata |
| `EVALUATE INFO.VIEW.TABLES() / .COLUMNS() / .MEASURES()` | **200** |
| `EVALUATE INFO.RELATIONSHIPS()` (bare) | **400** |
| `EVALUATE INFO.TABLES() / INFO.COLUMNS()` (bare) | **400** |
| `SUMMARIZECOLUMNS` across both tables | **200**, correctly grouped |
| `POST /admin/workspaces/getInfo` | **401** `PowerBINotAuthorizedException` |

Four findings, each load-bearing:

1. **`INFO.VIEW.*` works on the JSON `executeQueries` endpoint.** Microsoft's
   docs state INFO functions are unsupported there and our own prompt repeated
   it — true for the *bare* family, false for `INFO.VIEW.*`. This is the entire
   fix: relationships are readable with the querying identity's own token, no
   admin scope, no Premium capacity, no XMLA.
2. **`COLUMNSTATISTICS` returns hidden columns.** So the missing key columns
   were never a DAX limitation — they were dropped by our own `isHidden` filter,
   which localises defect 2 to the admin-scan path.
3. **The engine applies relationships at query time regardless of what we
   indexed.** The cross-table aggregation returned correct per-entity totals
   with no explicit join. The failure was always in *planning*, never execution:
   the agent could have answered the original question at any point.
4. **The admin scan is unavailable in this tenant** (401), which is exactly the
   configuration the connector must not depend on.

## The fix

- `_get_relationships_via_dax()` reads relationships via
  `INFO.VIEW.RELATIONSHIPS()` on the non-admin path, and for any dataset the
  admin scan described *without* relationships. Inactive relationships are
  dropped (the engine ignores them without `USERELATIONSHIP`).
- Failure is bounded: the first non-401/403/404 rejection sets
  `_info_functions_supported = False` and the rest of the crawl skips the call —
  one wasted request per client on an endpoint that doesn't support it. 401/403/404
  is treated as per-dataset (RLS, no Build permission) and does not disable the
  feature for other models.
- `_parse_admin_scan_tables` keeps hidden columns, flagged rather than dropped.
  `RowNumber-<GUID>` internal columns are still excluded.
- `_add_relationship_key_columns()` re-adds any column a relationship joins on
  that is missing from the discovered column list — a foreign key pointing at a
  column absent from the schema is worse than no foreign key.
- The prompt now distinguishes "not readable" from "does not exist", tells the
  agent the engine resolves joins itself, and documents the
  `INFO.VIEW.*` / bare-`INFO` split.
- Discovery logs relationship coverage, warning loudly when tables index with
  zero relationships — the condition that was previously silent.

## Verification

`backend/tests/unit/test_powerbi_relationships.py` (17 tests) covers parsing,
inactive-relationship handling, the kill-switch, per-dataset denial isolation,
hidden-column retention, internal-column exclusion, and key-column synthesis.

Live end-to-end through the real client against the tenant above:

```
before (INFO disabled):  fact_meals: cols=[fk_entity, meal_qty] fks=0
after:                   fact_meals: cols=[fk_entity, meal_qty] fks=1
                         foreign key (fk_entity) references RelHiddenProbe/dim_entity(primary_key)
                         cross-table query via the relationship: alpha 15.0, beta 7.0
```

Reproduce with `PBI_TENANT_ID` / `PBI_CLIENT_ID` / `PBI_CLIENT_SECRET` set
(secrets via env only, never committed). The probe workspace `bow-reltest` /
dataset `RelHiddenProbe` was created by this loop and can be deleted.

## Regression notes

- The `INFO.VIEW.` prefix is load-bearing and pinned by
  `TestRelationshipDaxShape`; swapping in the bare form silently reverts Power BI
  to join-less schemas.
- Incremental discovery reuses `fks` from the prior catalog, so models indexed
  before this change keep their empty relationships until a reindex that does
  not pass `prior_tables`.
