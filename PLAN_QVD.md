# QVD File Connector Plan

## Overview

Add QVD (QlikView Data) as a data source to read `.qvd` files from local filesystem or network shares. Each QVD file becomes a queryable table using SQL via DuckDB.

## Architecture

```
File System / Network Share
├── /data/qvd/
│   ├── Sales.qvd      → Table: sales
│   ├── Products.qvd   → Table: products
│   └── Customers.qvd  → Table: customers
```

## Design Decisions

### Query Approach

QVD files are data-only (no query engine). Two options:

| Approach | Pros | Cons |
|----------|------|------|
| **DuckDB + pandas** | SQL support, familiar, fast | Extra dependency on DuckDB |
| **pandas only** | Simple, direct | No SQL, only DataFrame ops |

**Recommendation:** Use DuckDB (already in project) to enable SQL queries on QVD data. Pattern matches existing DuckDB client.

### Flow

```
1. Read QVD file(s) with `qvd` package → pandas DataFrame
2. Register DataFrame as DuckDB view
3. Execute SQL queries via DuckDB
```

## Files to Create/Modify

### 1. New File: `backend/app/data_sources/clients/qvd_client.py`

```python
from app.data_sources.clients.base import DataSourceClient
import duckdb
import pandas as pd
from contextlib import contextmanager
from typing import Generator, List
from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
import qvd
import glob
import os


class QVDClient(DataSourceClient):
    """Read QVD (QlikView Data) files and query them via SQL."""

    def __init__(self, file_paths: str):
        """
        Args:
            file_paths: Newline-separated list of file paths or glob patterns.
                        e.g., "/data/*.qvd" or "/data/Sales.qvd\n/data/Products.qvd"
        """
        self.file_paths_raw = file_paths or ""
        self.patterns: List[str] = [
            p.strip() for p in self.file_paths_raw.splitlines() if p.strip()
        ]
        self._con: duckdb.DuckDBPyConnection | None = None

    def _resolve_files(self) -> List[str]:
        """Expand glob patterns to actual file paths."""
        files = []
        for pattern in self.patterns:
            matched = glob.glob(pattern, recursive=True)
            files.extend([f for f in matched if f.lower().endswith('.qvd')])
        return sorted(set(files))

    def _safe_table_name(self, filepath: str, used: set[str]) -> str:
        """Generate a safe table name from filepath."""
        import re
        basename = os.path.splitext(os.path.basename(filepath))[0]
        name = re.sub(r"[^a-zA-Z0-9_]+", "_", basename).strip("_").lower() or "table"
        original = name
        i = 1
        while name in used:
            i += 1
            name = f"{original}_{i}"
        used.add(name)
        return name

    def _load_qvd_files(self, con: duckdb.DuckDBPyConnection) -> dict[str, str]:
        """Load QVD files into DuckDB as views. Returns {table_name: filepath}."""
        files = self._resolve_files()
        used: set[str] = set()
        table_map: dict[str, str] = {}

        for filepath in files:
            table_name = self._safe_table_name(filepath, used)
            # Read QVD into pandas DataFrame
            df = qvd.read(filepath)
            # Register as DuckDB view
            con.register(table_name, df)
            table_map[table_name] = filepath

        return table_map

    @contextmanager
    def connect(self) -> Generator[duckdb.DuckDBPyConnection, None, None]:
        con: duckdb.DuckDBPyConnection | None = None
        try:
            con = duckdb.connect(database=":memory:")
            self._table_map = self._load_qvd_files(con)
            yield con
        except Exception as e:
            raise RuntimeError(f"Error connecting to QVD files: {e}")
        finally:
            if con is not None:
                con.close()

    def execute_query(self, sql: str) -> pd.DataFrame:
        with self.connect() as con:
            return con.execute(sql).df()

    def get_tables(self) -> List[Table]:
        tables: List[Table] = []
        with self.connect() as con:
            for table_name, filepath in self._table_map.items():
                cols: List[TableColumn] = []
                try:
                    desc = con.execute(f"DESCRIBE {table_name}").fetchall()
                    for d in desc:
                        cols.append(TableColumn(name=d[0], dtype=str(d[1])))
                except Exception:
                    pass
                tables.append(Table(
                    name=table_name,
                    columns=cols,
                    pks=[],
                    fks=[],
                    metadata_json={"qvd": {"source_file": filepath}}
                ))
        return tables

    def get_schemas(self) -> List[Table]:
        return self.get_tables()

    def get_schema(self, table_name: str) -> Table:
        cols: List[TableColumn] = []
        filepath = None
        with self.connect() as con:
            filepath = self._table_map.get(table_name)
            try:
                desc = con.execute(f"DESCRIBE {table_name}").fetchall()
                for d in desc:
                    cols.append(TableColumn(name=d[0], dtype=str(d[1])))
            except Exception:
                pass
        return Table(
            name=table_name,
            columns=cols,
            pks=[],
            fks=[],
            metadata_json={"qvd": {"source_file": filepath}}
        )

    def prompt_schema(self) -> str:
        return TableFormatter(self.get_schemas()).table_str

    def test_connection(self) -> dict:
        try:
            files = self._resolve_files()
            if not files:
                return {"success": False, "message": "No QVD files found matching the patterns"}
            # Try reading first file
            with self.connect() as con:
                con.execute("SELECT 1")
            return {
                "success": True,
                "message": f"Successfully loaded {len(files)} QVD file(s)"
            }
        except Exception as e:
            return {"success": False, "message": str(e)}

    @property
    def description(self) -> str:
        files = self._resolve_files()
        sample = ", ".join([os.path.basename(f) for f in files[:3]])
        if len(files) > 3:
            sample += ", ..."

        return f"""QVD files: {sample}

You can query these files using SQL (DuckDB syntax).

Examples:
```python
df = client.execute_query("SELECT * FROM sales LIMIT 10")
```
```python
df = client.execute_query("SELECT product, SUM(amount) AS total FROM sales GROUP BY product")
```
"""
```

### 2. Modify: `backend/app/schemas/data_sources/configs.py`

Add config and credentials classes.

```python
# QVD Config
class QVDConfig(BaseModel):
    file_paths: str = Field(
        ...,
        title="File Paths",
        description="QVD file paths or glob patterns (one per line). e.g., /data/*.qvd",
        json_schema_extra={"ui:type": "textarea"}
    )

# QVD has no credentials - just file access
class QVDCredentials(BaseModel):
    pass
```

### 3. Modify: `backend/app/schemas/data_source_registry.py`

Add registry entry.

```python
"qvd": DataSourceRegistryEntry(
    type="qvd",
    title="QVD Files",
    description="Query QlikView Data (.qvd) files using SQL.",
    config_schema=QVDConfig,
    credentials_auth=AuthOptions(
        default="none",
        by_auth={
            "none": AuthVariant(
                title="No Authentication",
                schema=QVDCredentials,
                scopes=["system"]
            )
        }
    ),
    client_path="app.data_sources.clients.qvd_client.QVDClient",
),
```

## Data Mapping

### QVD File → Table

```python
Table(
    name="sales",  # derived from filename: Sales.qvd → sales
    columns=[
        TableColumn(name="OrderID", dtype="BIGINT"),
        TableColumn(name="Product", dtype="VARCHAR"),
        TableColumn(name="Amount", dtype="DOUBLE"),
        # ... inferred from QVD
    ],
    pks=[],
    fks=[],
    metadata_json={
        "qvd": {
            "source_file": "/data/qvd/Sales.qvd"
        }
    }
)
```

## Dependencies

```
qvd          # QVD file reader (pip install qvd)
duckdb       # Already in project
pandas       # Already in project
```

The `qvd` package: https://pypi.org/project/qvd/

## Implementation Steps

### Phase 1: Config & Registry
- [ ] Add `QVDConfig` and `QVDCredentials` to configs.py
- [ ] Add imports and `__all__` exports
- [ ] Add registry entry to data_source_registry.py

### Phase 2: Client Implementation
- [ ] Create `qvd_client.py`
- [ ] Implement `_resolve_files()` - glob expansion
- [ ] Implement `_load_qvd_files()` - read QVD → register in DuckDB
- [ ] Implement `connect()` context manager
- [ ] Implement `execute_query(sql)`

### Phase 3: Schema Discovery
- [ ] Implement `get_tables()`
- [ ] Implement `get_schema(table_name)`
- [ ] Implement `prompt_schema()`

### Phase 4: Testing & Polish
- [ ] Implement `test_connection()`
- [ ] Implement `description` property
- [ ] Add `qvd` to requirements.txt
- [ ] Test with real QVD files
- [ ] Test connection form in UI

## Frontend

Add icon: `frontend/public/data_sources_icons/qvd.png`

## Notes

- QVD files are read-only extracts - no write capability needed
- Files are loaded into memory on connect - large files may need pagination
- Glob patterns allow flexible file selection (e.g., `/data/**/*.qvd` for recursive)
- Table names derived from filenames, sanitized for SQL compatibility

## Future Enhancements

- Support for remote file systems (S3, Azure Blob) via fsspec
- Lazy loading for large files
- QVD metadata extraction (table name, row count, fields from header)
