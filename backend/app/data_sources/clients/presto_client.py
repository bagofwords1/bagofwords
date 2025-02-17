from app.data_sources.clients.base import DataSourceClient
import pandas as pd
import sqlalchemy
from sqlalchemy import text
from contextlib import contextmanager
from typing import List, Generator
from app.ai.prompt_formatters import Table, TableColumn
from app.ai.prompt_formatters import TableFormatter
from functools import cached_property
import logging

# Configure logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


class PrestoClient(DataSourceClient):
    def __init__(self, host, port, catalog, schema, user, password=None, protocol="http"):
        """
        Initialize the Presto client.

        Args:
            host (str): Host of the Presto server.
            port (int): Port of the Presto server.
            catalog (str): Catalog to use.
            schema (str): Schema to use.
            user (str): Username for authentication.
            password (str): Password for authentication (used only for HTTPS).
            protocol (str): Protocol to use ("http" or "https"). Defaults to "http".
        """
        self.host = host
        self.port = port
        self.catalog = catalog
        self.schema = schema
        self.user = user
        self.password = password
        self.protocol = protocol

    @cached_property
    def presto_uri(self):
        """
        Generate the connection URI for Presto based on the protocol.
        Includes password for HTTPS but omits it for HTTP.
        """
        if self.protocol == "https":
            if not self.password:
                raise ValueError("Password is required for HTTPS connections.")
            uri = (
                f"trino://{self.user}:{self.password}@{self.host}:{self.port}/{self.catalog}/{self.schema}"
            )
        else:  # HTTP
            uri = (
                f"trino://{self.user}@{self.host}:{self.port}/{self.catalog}/{self.schema}"
            )
        logger.info(f"Constructed Presto URI: {uri}")
        return uri

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """
        Yield a connection to the Presto server.
        """
        engine = None
        conn = None
        try:
            engine = sqlalchemy.create_engine(self.presto_uri)
            conn = engine.connect()
            yield conn
        except Exception as e:
            logger.error(f"Error connecting to Presto: {e}")
            raise RuntimeError(f"{e}")
        finally:
            if conn is not None:
                conn.close()
            if engine is not None:
                engine.dispose()

    def execute_query(self, sql: str) -> pd.DataFrame:
        """
        Execute an SQL query and return the result as a DataFrame.

        Args:
            sql (str): The SQL query to execute.

        Returns:
            pd.DataFrame: Query result as a Pandas DataFrame.
        """
        try:
            with self.connect() as conn:
                df = pd.read_sql(text(sql), conn)
            return df
        except Exception as e:
            logger.error(f"Error executing SQL query: {e}")
            raise RuntimeError(f"{e}")

    def get_tables(self) -> List[Table]:
        """
        Retrieve all tables and their columns in the specified catalog and schema.

        Returns:
            List[Table]: List of tables with their column metadata.
        """
        try:
            with self.connect() as conn:
                sql = """
                    SELECT table_name, column_name, data_type
                    FROM information_schema.columns
                    WHERE table_catalog = :catalog
                    AND table_schema = :schema
                    ORDER BY table_name, ordinal_position
                """
                result = conn.execute(
                    text(sql), {"catalog": self.catalog, "schema": self.schema}
                ).fetchall()

                tables = {}
                for row in result:
                    table_name, column_name, data_type = row
                    if table_name not in tables:
                        tables[table_name] = Table(
                            name=table_name, columns=[], pks=None, fks=None
                        )
                    tables[table_name].columns.append(
                        TableColumn(name=column_name, dtype=data_type)
                    )
                return list(tables.values())
        except Exception as e:
            logger.error(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table_id: str) -> Table:
        """
        Retrieve metadata for a specific table.
        This method is now obsolete and should not be used.
        """
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead."
        )

    def get_schemas(self):
        """
        Retrieve schemas for all tables in the specified catalog and schema.

        Returns:
            List[Table]: List of table metadata for the current schema.
        """
        return self.get_tables()

    def prompt_schema(self):
        """
        Format the schema for display or prompting.
        """
        schemas = self.get_tables()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """
        Test the connection to Presto and return status information.

        Returns:
            dict: Connection test result with success status and message.
        """
        try:
            with self.connect() as conn:
                conn.execute(text("SELECT 1"))
                return {"success": True, "message": "Successfully connected to Presto"}
        except Exception as e:
            logger.error(f"Error testing Presto connection: {e}")
            return {"success": False, "message": str(e)}

    @property
    def description(self):
        """
        Generate a description of the Presto client for documentation or display.
        """
        system_prompt = """
        You can call the execute_query method to run SQL queries.

        The below are examples for how to use the execute_query method. Note that the actual SQL will vary based on the schema.
        Notice only the SQL syntax and instructions on how to use the execute_query method, not the actual SQL queries.

        ```python
        df = client.execute_query("SELECT * FROM users")
        ```
        or:
        ```python
        df = client.execute_query("SELECT * FROM users WHERE age > 30")
        ```
        """
        description = (
            f"Presto database at {self.host}:{self.port}/{self.catalog}/{self.schema}\n\n"
        )
        description += system_prompt
        return description
