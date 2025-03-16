from app.data_sources.clients.base import DataSourceClient
import pandas as pd
from typing import List
from app.ai.prompt_formatters import Table, TableColumn
import logging
import awswrangler as wr
import boto3
from tenacity import retry, stop_after_attempt, wait_fixed

# Configure logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

def log_before_retry(retry_state) -> None:
    """Logs a message before each retry attempt."""
    logger.info("Retrying %s (attempt %d)", retry_state.fn.__name__, retry_state.attempt_number)

class AwsAthenaClient(DataSourceClient):
    def __init__(
        self,
        region: str,
        database: str,
        s3_output_location: str,
        access_key: str,
        secret_key: str,
        workgroup: str = "primary",
        catalog: str = "AwsDataCatalog",
        retry_wait_seconds: int = 0,
        retry_max_attempts: int = 0,
    ):
        """
        Initialize the Athena client using AWS Wrangler.

        Args:
            region (str): AWS region
            database (str): The name of the database
            s3_output_location (str): S3 location for query results
            access_key (str): AWS access key ID
            secret_key (str): AWS secret access key
            workgroup (str): Athena workgroup to use
            catalog (str): Athena catalog name
            retry_wait_seconds (int): Seconds to wait before each retry
            retry_max_attempts (int): Maximum number of retry attempts
        """
        self.database = database
        self.s3_output_location = s3_output_location
        self.workgroup = workgroup
        self.catalog = catalog
        self.retry_wait_seconds = retry_wait_seconds
        self.retry_max_attempts = retry_max_attempts
        
        # Create boto3 session
        self.boto3_session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region
        )

        # Keep glue client for schema operations
        self.glue_client = self.boto3_session.client('glue')

    @retry(
        wait=wait_fixed(0),
        stop=stop_after_attempt(0),
        before=log_before_retry,
    )
    def execute_query(self, sql: str) -> pd.DataFrame:
        """
        Execute an SQL query and return the result as a DataFrame using AWS Wrangler.

        Args:
            sql (str): The SQL query to execute.

        Returns:
            pd.DataFrame: Query result as a Pandas DataFrame.
        """
        try:
            logger.info("Executing query: %s", sql)
            df = wr.athena.read_sql_query(
                sql=sql,
                database=self.database,
                ctas_approach=False,  # Disable CREATE TABLE AS approach for better performance
                s3_output=self.s3_output_location,
                workgroup=self.workgroup,
                boto3_session=self.boto3_session,
                data_source=self.catalog
            )
            logger.info("Query executed successfully, returned %d rows", len(df))
            return df
        except Exception as e:
            logger.error("Error executing SQL query: %s", str(e), exc_info=True)
            raise RuntimeError(f"Query execution failed: {str(e)}")

    def test_connection(self) -> dict:
        """
        Test the connection to Athena by running a simple query.

        Returns:
            dict: Connection test result with success status and message.
        """
        try:
            self.execute_query("SHOW TABLES")
            return {"success": True, "message": "Successfully connected to Athena"}
        except Exception as e:
            logger.error("Error testing Athena connection: %s", str(e))
            return {"success": False, "message": str(e)}

    @property
    def description(self) -> str:
        """
        Generate a description of the Athena client for documentation or display.
        """
        system_prompt = """
        You can call the execute_query method to run SQL queries.

        Examples:
        ```python
        # List all tables
        df = client.execute_query("SHOW TABLES")

        # Query specific table
        df = client.execute_query("SELECT * FROM my_table LIMIT 10")
        
        # Show table schema
        df = client.execute_query("DESCRIBE my_table")
        ```
        """
        description = f"AWS Athena database: {self.database}\n\n"
        description += system_prompt
        return description

    def get_tables(self) -> List[Table]:
        """
        Retrieve all tables and their columns in the specified database and catalog.

        Returns:
            List[Table]: List of tables with their column metadata.
        """
        try:
            # Use the existing Glue client instance
            paginator = self.glue_client.get_paginator('get_tables')
            tables = {}
            for page in paginator.paginate(DatabaseName=self.database):
                for table in page['TableList']:
                    table_name = table['Name']
                    tables[table_name] = Table(
                        name=table_name,
                        columns=[],
                        pks=None,
                        fks=None
                    )
                    
                    # Add columns from StorageDescriptor
                    if 'StorageDescriptor' in table and 'Columns' in table['StorageDescriptor']:
                        for col in table['StorageDescriptor']['Columns']:
                            tables[table_name].columns.append(
                                TableColumn(name=col['Name'], dtype=col['Type'])
                            )
                    
                    # Add partition columns if any
                    for partition in table.get('PartitionKeys', []):
                        tables[table_name].columns.append(
                            TableColumn(name=partition['Name'], dtype=partition['Type'])
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
        Retrieve schemas for all tables in the specified database.
        """
        return self.get_tables()

    def prompt_schema(self):
        """
        Format the schema for display or prompting.
        """
        schemas = self.get_tables()
        return TableFormatter(schemas).table_str
