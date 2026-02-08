from __future__ import annotations

import glob
import os
import re
from contextlib import contextmanager
from typing import Generator, List

import duckdb
import pandas as pd

from app.ai.prompt_formatters import Table, TableColumn, TableFormatter
from app.data_sources.clients.base import DataSourceClient


class QVDClient(DataSourceClient):
    """Read QVD (QlikView Data) files and query them via SQL using DuckDB."""

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
        self._table_map: dict[str, str] = {}

    def _resolve_files(self) -> List[str]:
        """Expand glob patterns to actual file paths."""
        files = []
        for pattern in self.patterns:
            matched = glob.glob(pattern, recursive=True)
            files.extend([f for f in matched if f.lower().endswith('.qvd')])
        return sorted(set(files))

    def _safe_table_name(self, filepath: str, used: set[str]) -> str:
        """Generate a safe table name from filepath."""
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
        try:
            from pyqvd import read_qvd
        except ImportError:
            raise ImportError(
                "The 'pyqvd' package is required for QVD support. "
                "Install it with: pip install pyqvd"
            )

        files = self._resolve_files()
        used: set[str] = set()
        table_map: dict[str, str] = {}

        for filepath in files:
            table_name = self._safe_table_name(filepath, used)
            # Read QVD into pandas DataFrame
            df = read_qvd(filepath)
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
                return {
                    "success": False,
                    "message": "No QVD files found matching the patterns"
                }
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
