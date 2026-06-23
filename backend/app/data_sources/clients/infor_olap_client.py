from app.data_sources.clients.xmla_base import XmlaClient


class InforOlapClient(XmlaClient):
    """
    Infor d/EPM OLAP client (formerly Infor BI / MIS Alea OLAP).

    Talks to the Infor OLAP XMLA Provider — the supported entry point for
    on-premise d/EPM (native connections were removed; XMLA is mandatory).
    All XMLA transport, discovery, and query execution live in ``XmlaClient``;
    this subclass only supplies Infor-specific labels and the MDX prompt.

    Each cube is exposed as one schema table named ``Catalog/Cube`` whose
    columns are the cube's hierarchies (dimensions) and measures. The unique
    names needed to author MDX are carried in each column's ``metadata``.
    """

    META_KEY = "infor_olap"
    PRODUCT_NAME = "Infor OLAP"
    EMPTY_NOTE = "No OLAP databases visible to this user — check application access."
    QUERY_REQUIRED_MSG = "MDX query is required"

    @property
    def description(self) -> str:
        return (
            "Infor OLAP Client: discover cubes (XMLA Discover) and execute MDX "
            "against the Infor d/EPM OLAP semantic layer (XMLA Execute). Works "
            "with the on-premise Infor OLAP XMLA Provider."
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        return """

## Infor OLAP MDX Guide

Execute MDX queries against Infor d/EPM OLAP cubes. The OLAP server resolves
MDX against the multidimensional semantic model (dimensions, hierarchies, and
measures).

### Schema Structure

Each cube is exposed as a schema table named `Catalog/Cube`:
- `Finance/GL` - the GL cube in the Finance catalog
- `Sales/Revenue` - the Revenue cube in the Sales catalog

Each column is either a dimension hierarchy (`dtype="dimension"`) or a measure
(`dtype="measure"`). The MDX `unique_name` for every column lives in its
`metadata.unique_name` (e.g. `[Time].[Calendar]`, `[Measures].[Sales Amount]`),
and the cube's unique name is in `metadata.infor_olap.cubeUniqueName`.

### How to Execute Queries

**Signature**: `execute_query(mdx_query, table_name)` — pass the `Catalog/Cube`
schema table name as the second argument so the catalog can be resolved.

```python
df = db_clients['infor_olap'].execute_query(
    '''
    SELECT
      { [Measures].[Sales Amount] } ON COLUMNS,
      NON EMPTY { [Product].[Category].Members } ON ROWS
    FROM [Revenue]
    ''',
    "Sales/Revenue"
)
```

### MDX Rules

- The FROM clause names the cube in brackets: `FROM [Revenue]`.
- Put measures on one axis (usually COLUMNS) and dimension members on the
  other (usually ROWS).
- Reference members by their unique name from `metadata.unique_name`, e.g.
  `[Product].[Category].Members`, `[Time].[2025]`.
- Use `NON EMPTY` to drop empty rows and `CROSSJOIN(...)` to combine
  dimensions on one axis.
- Use MDX functions (`Members`, `Children`, `Descendants`, `Filter`,
  `Order`, `TopCount`) rather than SQL syntax — this is MDX, not SQL.
"""
