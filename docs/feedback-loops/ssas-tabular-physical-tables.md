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
- Parse active many-to-one CSDL associations into BOW foreign keys. Preserve
  inactive role-playing relationships as semantic metadata, without
  misrepresenting them as active foreign keys, so the agent can use DAX
  `USERELATIONSHIP` when appropriate.
- Preserve CSDL keys, format strings, nullability, length/precision, content
  types, and other structural annotations in indexed metadata.
- When the connection account is an Analysis Services administrator, enrich
  the CSDL baseline with TMSCHEMA measures, calculated columns, hierarchies,
  levels, relationships, partition summaries, KPIs, roles, perspectives, and
  cultures. A denied admin probe immediately and safely falls back to CSDL.
- Keep sensitive partition source queries out of indexed metadata.
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

Six active relationships were indexed as foreign keys. All eight model
relationships were retained as metadata, including the two inactive Date
role-playing relationships. The read-only CSDL path also retained one declared
primary key and 27 formatted fields. A live DAX
`EVALUATE ROW("Internet Total Sales", [Internet Total Sales])` query returned a
non-null result through the connector.

The live reader account is intentionally not an Analysis Services
administrator. Its `TMSCHEMA_TABLES` permission probe was denied, after which
the connector returned the complete CSDL baseline without attempting the
remaining privileged rowsets. The privileged enrichment path is covered with
a representative TMSCHEMA fixture that checks expressions, descriptions,
display folders, sort-by columns, hierarchies and levels, relationship flags,
partition summaries, roles, perspectives, and cultures.

After granting the same account Full Control on the model database, a live UI
reindex changed all seven tables to `metadata_source=CSDL+TMSCHEMA` and
persisted 130 fields (107 physical columns and 23 measures), 24 DAX
expressions, four hierarchies, 11 partition summaries, two KPIs, four roles,
one perspective, and all eight relationships. Partition summaries contained
only `name`, `mode`, and `state`; source queries were not stored.

Automated verification:

```text
108 passed — SSAS, shared XMLA providers, and schema-context unit tests
11 passed  — generic connection and data-source E2E tests on SQLite
171 passed — Power BI client, Report Server, relationships, context, and access tests
```

## Local product and LLM verification

The connection was created through the real local product API and used by the
live frontend. Connection testing reported `Connected successfully. Found 7
tables.`; indexing completed with seven tables. A subsequent reindex through
the live Agents UI persisted the CSDL source marker, 27 format annotations,
all eight relationships (including two inactive), and the declared key. All
seven tables were activated for a dedicated Adventure Works agent.

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

After metadata enrichment, a second report using the deployment's configured
model selected the physical `Internet Sales` table and executed:

```DAX
EVALUATE ROW(
    "Internet Total Sales",
    [Internet Total Sales]
)
```

It returned one formatted value (`$29,358,677.22`) and completed without an
execution error.

With the privileged metadata indexed, a hierarchy-aware prompt selected the
physical `Date` and `Internet Sales` tables and generated:

```DAX
EVALUATE
SUMMARIZECOLUMNS(
    'Date'[Calendar Year],
    "Internet Total Sales", [Internet Total Sales]
)
ORDER BY 'Date'[Calendar Year] ASC
```

It returned five correctly ordered and formatted rows for 2010–2014 without
an execution error.

## Regression boundaries

- Failure or absence of CSDL metadata deliberately falls back to the existing
  cube/hierarchy/measure discovery, preserving Multidimensional SSAS behavior.
- The shared `XmlaClient` contract used by Infor OLAP and SAP BW is unchanged;
  its old implementation was extracted into a helper and its tests remain
  green.
- No Power BI code or connection data is changed. The SSAS query-time metadata
  attachment mirrors that already-working connector without sharing its API or
  credentials.
