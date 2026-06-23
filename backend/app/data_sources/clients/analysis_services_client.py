from app.data_sources.clients.xmla_base import XmlaClient
from typing import Dict, Optional
import pandas as pd


class AnalysisServicesClient(XmlaClient):
    """
    Microsoft SQL Server Analysis Services (SSAS) client.

    Connects to the SSAS XMLA endpoint (typically the IIS msmdpump.dll pump,
    e.g. ``https://server/olap/msmdpump.dll``) over HTTP with Basic auth and
    supports both SSAS model types:

      - Multidimensional models — queried with MDX.
      - Tabular models — queried with DAX (native) or MDX.

    All XMLA transport/discovery lives in ``XmlaClient``. This subclass adds
    per-catalog model-type detection (so the agent uses a supported dialect)
    and a guard that rejects DAX against a Multidimensional cube.
    """

    META_KEY = "analysis_services"
    PRODUCT_NAME = "Microsoft Analysis Services"
    EMPTY_NOTE = "No databases visible to this user — check permissions."
    QUERY_REQUIRED_MSG = "An MDX or DAX query is required"

    # ------------------------------------------------------------------
    # Model-type detection
    # ------------------------------------------------------------------

    def _catalog_context(self, catalog: str) -> Dict:
        """Detect whether a catalog is Tabular or Multidimensional.

        Tabular models (compatibility level 1200+) expose the ``TMSCHEMA_*``
        metadata DMVs; Multidimensional has none. We probe ``TMSCHEMA_MODEL``
        and treat success as Tabular. Any failure falls back to
        Multidimensional, which is always safe because MDX works on both —
        only DAX is Tabular-only.
        """
        try:
            self._execute_statement("SELECT * FROM $SYSTEM.TMSCHEMA_MODEL", catalog)
            return {"modelType": "TABULAR", "supportsDax": True}
        except Exception:
            return {"modelType": "MULTIDIMENSIONAL", "supportsDax": False}

    @staticmethod
    def _is_dax(query: str) -> bool:
        """A DAX query statement starts with EVALUATE or DEFINE."""
        head = (query or "").lstrip().upper()
        return head.startswith("EVALUATE") or head.startswith("DEFINE")

    # ------------------------------------------------------------------
    # Query execution (adds the DAX-on-Multidimensional guard)
    # ------------------------------------------------------------------

    def execute_query(
        self,
        query: str,
        table_name: Optional[str] = None,
        catalog: Optional[str] = None,
        max_rows: Optional[int] = None,
    ) -> pd.DataFrame:
        """
        Execute an MDX or DAX statement via XMLA Execute and return a DataFrame.

        Args:
            query: an MDX SELECT or a DAX EVALUATE statement.
            table_name: optional ``Catalog/Cube`` hint. Used to resolve the
                catalog and, for DAX, to verify the target is a Tabular model.
            catalog: explicit catalog override (takes precedence).
            max_rows: optional client-side row cap.
        """
        if not query or not query.strip():
            raise ValueError(self.QUERY_REQUIRED_MSG)
        self.connect()

        table = None
        if table_name:
            try:
                table = self.get_schema(table_name)
            except Exception:
                table = None

        # Guard: DAX only runs on Tabular models. When we know the target's
        # model type and it is Multidimensional, reject DAX with a clear error
        # instead of surfacing a cryptic server fault.
        if self._is_dax(query) and table is not None:
            meta = (table.metadata_json or {}).get(self.META_KEY) or {}
            if not meta.get("supportsDax"):
                raise RuntimeError(
                    "DAX queries are only supported on Tabular models; this "
                    "target is Multidimensional — use MDX instead."
                )

        target_catalog = catalog or self.catalog
        if not target_catalog and table is not None:
            target_catalog = ((table.metadata_json or {}).get(self.META_KEY) or {}).get("catalog")

        rows = self._execute_statement(query, target_catalog)
        return self._rows_to_df(rows, max_rows)

    # ------------------------------------------------------------------
    # Prompt / description
    # ------------------------------------------------------------------

    @property
    def description(self) -> str:
        return (
            "Microsoft Analysis Services Client: discover cubes/models (XMLA "
            "Discover) and execute MDX or DAX against SSAS (XMLA Execute). "
            "Supports both Multidimensional (MDX) and Tabular (DAX/MDX) models."
        ) + self.system_prompt()

    def system_prompt(self) -> str:
        return """

## Microsoft Analysis Services (SSAS) Query Guide

Execute queries against SSAS cubes and models over XMLA. SSAS has two model
types and they accept different query languages:

- **Multidimensional** models accept **MDX only**.
- **Tabular** models accept **DAX** (preferred) and also MDX.

### Schema Structure

Each cube/model is exposed as a schema table named `Catalog/Cube`:
- `AdventureWorks/Sales` - the Sales cube in the AdventureWorks catalog

Every table records its model type in
`metadata.analysis_services.modelType` (`MULTIDIMENSIONAL` or `TABULAR`) and
`metadata.analysis_services.supportsDax`. **Pick the language from the model
type**: write MDX for Multidimensional; for Tabular, prefer DAX. When unsure,
MDX is always safe.

Each column is a dimension hierarchy (`dtype="dimension"`) or a measure
(`dtype="measure"`); its query unique name is in `metadata.unique_name`.

### How to Execute Queries

**Signature**: `execute_query(query, table_name)` — pass the `Catalog/Cube`
schema table name as the second argument (resolves the catalog and validates
DAX vs the model type).

```python
# MDX (works on either model type)
df = db_clients['analysis_services'].execute_query(
    '''
    SELECT { [Measures].[Sales Amount] } ON COLUMNS,
           NON EMPTY { [Product].[Category].Members } ON ROWS
    FROM [Sales]
    ''',
    "AdventureWorks/Sales"
)

# DAX (Tabular models only)
df = db_clients['analysis_services'].execute_query(
    '''
    EVALUATE
    SUMMARIZECOLUMNS(
        Product[Category],
        "Sales", SUM(Sales[SalesAmount])
    )
    ''',
    "AdventureWorks/Sales"
)
```

### Rules

- **MDX**: FROM names the cube in brackets (`FROM [Sales]`); measures on one
  axis, dimension members on the other; use `NON EMPTY`; reference members by
  their `metadata.unique_name`.
- **DAX**: start with `EVALUATE`; use `SUMMARIZECOLUMNS`, `FILTER`,
  `CALCULATE`, `TOPN`; reference columns as `Table[Column]` and measures as
  `[Measure]`.
- Never send DAX to a Multidimensional model — it is not supported there.
"""
