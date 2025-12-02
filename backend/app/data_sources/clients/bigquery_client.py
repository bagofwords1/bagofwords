from app.data_sources.clients.base import DataSourceClient

import json
import os
import pandas as pd
from google.cloud import bigquery
from google.oauth2 import service_account
from typing import List, Generator, Optional
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from contextlib import contextmanager


class BigqueryClient(DataSourceClient):
    def __init__(self, project_id, credentials_json, dataset, maximum_bytes_billed: Optional[int] = None, use_query_cache: bool = False):
        self.project_id = project_id
        self.credentials_json = credentials_json
        self.dataset = dataset
        # Accept comma-separated datasets; normalize and dedupe while preserving order
        self._datasets = []
        if isinstance(self.dataset, str) and self.dataset.strip():
            parts = [s.strip() for s in self.dataset.split(",") if s.strip()]
            seen = set()
            for p in parts:
                if p not in seen:
                    seen.add(p)
                    self._datasets.append(p)
        # Primary dataset for legacy expectations
        self._primary_dataset = (self._datasets[0] if self._datasets else (self.dataset if isinstance(self.dataset, str) and self.dataset.strip() else None))
        self.maximum_bytes_billed = maximum_bytes_billed
        self.use_query_cache = use_query_cache

        # Support both raw JSON content and a server-accessible file path
        self.credentials = None
        if isinstance(self.credentials_json, str):
            # Try to parse as JSON content first (preferred & more secure)
            try:
                info = json.loads(self.credentials_json)
                if not isinstance(info, dict):
                    raise ValueError("Parsed credentials JSON is not an object")
                self.credentials = service_account.Credentials.from_service_account_info(info)
            except (json.JSONDecodeError, ValueError):
                # Fall back to treating the string as a file path on the server
                if not os.path.isfile(self.credentials_json):
                    raise ValueError(
                        "Provided BigQuery credentials are neither valid JSON content nor a readable server file path."
                    )
                self.credentials = service_account.Credentials.from_service_account_file(self.credentials_json)
        else:
            raise TypeError("credentials_json must be a JSON string or a server file path string")

        self.client = bigquery.Client(project=self.project_id, credentials=self.credentials)

    @contextmanager
    def connect(self) -> Generator[bigquery.Client, None, None]:
        """Yield a connection to BigQuery."""
        try:
            yield self.client
        finally:
            # No explicit close method for BigQuery client, but ensuring resource cleanup if needed
            pass

    def execute_query(self, sql: str, maximum_bytes_billed: Optional[int] = None, use_query_cache: Optional[bool] = None) -> pd.DataFrame:
        """Run SQL statement and return the result as a DataFrame.

        Args:
            sql: SQL to execute.
            maximum_bytes_billed: Optional per-call cap. If 0 or None, no cap. Defaults to client-level setting.
            use_query_cache: Optional per-call cache flag. Defaults to client-level setting.
        """
        try:
            with self.connect() as conn:
                # Determine effective settings
                cap = self.maximum_bytes_billed if maximum_bytes_billed is None else maximum_bytes_billed
                cache_flag = self.use_query_cache if use_query_cache is None else use_query_cache

                job_config = bigquery.QueryJobConfig(use_query_cache=bool(cache_flag))
                # Only set maximum_bytes_billed if a positive integer cap is provided
                if isinstance(cap, int) and cap > 0:
                    job_config.maximum_bytes_billed = int(cap)

                query_job = conn.query(sql, job_config=job_config)
                result = query_job.result()
                df = result.to_dataframe()
            return df
        except Exception as e:
            print(f"Error executing SQL: {e}")
            raise e

    def get_tables(self) -> List[Table]:
        """Get all tables and their columns across one or more datasets.
        - Supports comma-separated datasets via the existing `dataset` config field.
        - If no dataset provided, auto-discovers all datasets in the project.
        - Emits fully qualified table names: DATASET.TABLE
        """
        try:
            with self.connect() as conn:
                # Determine datasets to scan
                if self._datasets:
                    datasets = self._datasets
                else:
                    datasets = [d.dataset_id for d in conn.list_datasets()]

                tables = {}
                for ds in datasets:
                    sql = f"""
                        SELECT table_name, column_name, data_type
                        FROM `{self.project_id}.{ds}.INFORMATION_SCHEMA.COLUMNS`
                        ORDER BY table_name, ordinal_position
                    """
                    query_job = conn.query(sql)
                    results = query_job.result().to_dataframe()

                    for _, row in results.iterrows():
                        table_name = row["table_name"]
                        column_name = row["column_name"]
                        data_type = row["data_type"]

                        key = (ds, table_name)
                        fqn = f"{ds}.{table_name}"
                        if key not in tables:
                            tables[key] = Table(
                                name=fqn, columns=[], pks=None, fks=None, metadata_json={"dataset": ds}
                            )
                        tables[key].columns.append(TableColumn(name=column_name, dtype=data_type))

                return list(tables.values())
        except Exception as e:
            print(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    def get_schemas(self):
        """Get schemas for all tables in the specified dataset."""
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to BigQuery and return status information."""
        try:
            with self.connect() as conn:
                datasets = list(conn.list_datasets())
                if datasets:
                    return {
                        "success": True,
                        "message": "Successfully connected to BigQuery"
                    }
                else:
                    return {
                        "success": True,
                        "message": "Successfully connected to BigQuery, but no datasets found"
                    }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):

        system_prompt = """
        You can call the execute_query method to run BigQuery compatible SQL queries.

        The below are examples for how to use the execute_query method. Note that the actual SQL will vary based on the schema.
        Notice only the SQL syntax and instructions on how to use the execute_query method, not the actual SQL queries.

        ```python
        df = client.execute_query("SELECT * FROM users")
        ```
        or:
        ```python
        df = client.execute_query("SELECT * FROM users WHERE age > 30")
        ```

        BigQuery specific syntax notes:
        - Don’t use reserved keywords as aliases (e.g., AT, ON, IN, ALL, GROUP, ORDER, JOIN, WHERE, SELECT).
        - No trailing commas in SELECT or CTEs.
        - Every JOIN must have an ON/USING clause.
        - All non-aggregated columns must be in GROUP BY.
        - Use QUALIFY for filtering window functions.
        - Always use partitions when available in context/metadata resources.
        """


        description = f"BigQuery project {self.project_id}; datasets: {self._datasets}\n\n"
        description += system_prompt
        return description
        if self._datasets:
            listed = ", ".join(self._datasets)
            return f"BigQuery project {self.project_id}; datasets: {listed}"
        return f"BigQuery project {self.project_id}; datasets: all"
