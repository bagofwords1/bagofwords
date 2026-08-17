# Feedback Loop — SSAS Tabular exposed perspectives instead of tables

The Analysis Services connector treated every XMLA endpoint like a
Multidimensional cube. For the Adventure Works Tabular backup this indexed the
model and its perspective as two BOW tables, while hiding the seven physical
model tables, their columns, measures, and relationships. The agent then copied
the old prompt's example cube name (`Sales`) into an MDX query and received
`The Sales cube does not exist`.

The existing Multidimensional behavior remains the fallback. The change is
specific to SSAS catalogs that successfully expose Tabular CSDL metadata;
other XMLA connectors continue using the shared cube-shaped discovery path.

## Reproduction

Live endpoint: SQL Server Analysis Services with the Microsoft Adventure Works
Internet Sales Tabular model restored from the compatibility-level 1200 ABF.
The connection uses an ordinary read-only database account over `msmdpump.dll`.

Before the fix, `MDSCHEMA_CUBES` produced only:

```text
Adventure Works Internet Sales/Adventure Works Internet Sales Model
Adventure Works Internet Sales/Internet Sales
```

The connector's `TMSCHEMA_MODEL` probe could not identify the model because
that DMV requires Analysis Services administrator permission. Falling back to
`MULTIDIMENSIONAL` was therefore a false classification for a valid read-only
Tabular connection.

The regression test fixes this boundary in place: its read-only fixture rejects
the administrative DMV, returns a model plus perspective from
`MDSCHEMA_CUBES`, and expects physical tables from `DISCOVER_CSDL_METADATA`.
The test failed before the implementation with the two cube/perspective names.

## The fix

- Discover `DISCOVER_CSDL_METADATA`, which is available to a database reader,
  before using the historic cube discovery path.
- Map CSDL entity sets and types to one BOW table per physical Tabular table.
- Preserve exact display names and DAX identifiers for columns and measures.
- Parse active many-to-one CSDL associations into BOW foreign keys; inactive
  role-playing relationships are omitted rather than represented as active.
- Mark Tabular tables with `modelType=TABULAR`, `supportsDax=true`, and
  `preferredDialect=DAX`. Multidimensional fallback tables are marked `MDX`.
- Reuse indexed table metadata during execution, following the existing Power
  BI client pattern, so a query does not need another complete metadata crawl.
- Tell the agent to use the selected physical table names and DAX for Tabular,
  while preserving MDX instructions for Multidimensional models.

## Live verification

Directly through `AnalysisServicesClient`, the same read-only account now
discovers:

```text
Adventure Works Internet Sales/Customer             25 columns
Adventure Works Internet Sales/Date                 18 columns, 2 measures
Adventure Works Internet Sales/Geography             8 columns
Adventure Works Internet Sales/Product              27 columns
Adventure Works Internet Sales/Product Category      3 columns
Adventure Works Internet Sales/Product Subcategory   4 columns
Adventure Works Internet Sales/Internet Sales       45 columns, 21 measures
```

Six active relationships were indexed across the related tables. A live DAX
`EVALUATE TOPN(3, 'Product')` query returned three product rows through the
connector.

Automated verification:

```text
81 passed  — SSAS, shared XMLA providers, and schema-context unit tests
11 passed  — generic connection and data-source E2E tests on SQLite
148 passed — Power BI client, Report Server, relationships, context, and access tests
```

## Local product and LLM verification

The connection was created through the real local product API and used by the
live frontend. Connection testing reported `Connected successfully. Found 7
tables.`; indexing completed with seven tables. All seven were activated for a
dedicated Adventure Works agent.

The attached report used the deployment's configured model. For
"top 5 product categories by Internet Total Sales," the model selected the
physical `Internet Sales` and `Product` tables and generated DAX using the
semantic measure and dimension:

```DAX
EVALUATE
TOPN(
    5,
    SUMMARIZECOLUMNS(
        'Product'[Product Category Name],
        "Sales Amount", [Internet Total Sales]
    ),
    [Sales Amount], DESC,
    'Product'[Product Category Name], ASC
)
```

SSAS returned three categories: Bikes ($28,318,144.65), Accessories
($700,759.96), and Clothing ($339,772.61). The final clean report completed
from one prompt in two agent steps with `create_data.success=true`, three rows,
and no execution errors.

## Regression boundaries

- Failure or absence of CSDL metadata deliberately falls back to the existing
  cube/hierarchy/measure discovery, preserving Multidimensional SSAS behavior.
- The shared `XmlaClient` contract used by Infor OLAP and SAP BW is unchanged;
  its old implementation was extracted into a helper and its tests remain
  green.
- No Power BI code or connection data is changed. The SSAS query-time metadata
  attachment mirrors that already-working connector without sharing its API or
  credentials.
