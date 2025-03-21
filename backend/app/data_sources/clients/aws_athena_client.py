from app.data_sources.clients.base import DataSourceClient
import pandas as pd
from typing import List
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
import logging
import awswrangler as wr
import boto3
from tenacity import retry, stop_after_attempt, wait_fixed
import threading
from datetime import datetime, timedelta, timezone
import os

# Configure logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

class SessionManager:
    """Handles AWS session management with role assumption as a thread-safe singleton."""

    _instance = None
    _lock = threading.Lock()  # For thread-safety

    def __new__(cls, role_arn: str, region: str, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(SessionManager, cls).__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self, role_arn: str, region: str) -> None:
        if hasattr(self, '_initialized') and self._initialized:
            return
        self.role_arn = role_arn
        self.region = region
        self._renew_session()
        self._initialized = True

    def _renew_session(self) -> None:
        """Assume the role and create a new session."""
        logger.info("Renewing AWS session.")
        sts_client = boto3.client("sts", region_name=self.region)
        role_arn = f"{self.role_arn}-{os.getenv('DEPLOYED_ENV', '').title()}" if os.getenv('DEPLOYED_ENV') else self.role_arn
        assumed_role = sts_client.assume_role(
            RoleArn=role_arn,
            RoleSessionName="AthenaClientSession",
        )
        credentials = assumed_role["Credentials"]
        self.credentials_expiration = credentials["Expiration"] - timedelta(minutes=5)
        self.session = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=self.region
        )

    def _ensure_session_valid(self) -> None:
        """Ensure that the current AWS session is valid; renew if expired."""
        current_time = datetime.now(timezone.utc)
        if current_time >= self.credentials_expiration:
            logger.info("AWS session has expired, renewing...")
            self._renew_session()

    def __enter__(self):
        """Enter the context manager, ensuring the session is valid."""
        self._ensure_session_valid()
        return self.session

    def __exit__(self, exc_type, exc_value, traceback):
        """Exit the context manager."""
        pass

def log_before_retry(retry_state) -> None:
    """Logs a message before each retry attempt."""
    logger.info("Retrying %s (attempt %d)", retry_state.fn.__name__, retry_state.attempt_number)

class AwsAthenaClient(DataSourceClient):
    def __init__(
        self,
        region: str,
        database: str,
        s3_output_location: str,
        access_key: str = None,
        secret_key: str = None,
        role_arn: str = None,
        workgroup: str = "primary",
        data_source: str = "AwsDataCatalog",
        retry_wait_seconds: int = 0,
        retry_max_attempts: int = 0,
        result_reuse_enable: bool = False,
        result_reuse_minutes: int = 60,
        encryption_option: str = None,
        kms_key: str = None
    ):
        """
        Initialize the Athena client using AWS Wrangler.

        Args:
            region (str): AWS region
            database (str): The name of the database
            s3_output_location (str): S3 location for query results
            access_key (str, optional): AWS access key ID
            secret_key (str, optional): AWS secret access key
            role_arn (str, optional): AWS IAM Role ARN to assume
            workgroup (str): Athena workgroup to use
            data_source (str): Athena data source name
            retry_wait_seconds (int): Seconds to wait before each retry
            retry_max_attempts (int): Maximum number of retry attempts
            result_reuse_enable (bool): Whether to enable query result reuse
            result_reuse_minutes (int): How long to reuse cached results
            encryption_option (str): S3 encryption option
            kms_key (str): KMS key for encryption
        """
        self.database = database
        self.s3_output_location = s3_output_location
        self.workgroup = workgroup
        self.data_source = data_source
        self.retry_wait_seconds = retry_wait_seconds
        self.retry_max_attempts = retry_max_attempts
        self.region = region
        self.result_reuse_enable = result_reuse_enable
        self.result_reuse_minutes = result_reuse_minutes
        self.encryption_option = encryption_option
        self.kms_key = kms_key
        
        if role_arn:
            self.session_manager = SessionManager(role_arn, region)
            # Initialize session for Glue client
            with self.session_manager as session:
                self.glue_client = session.client('glue', region_name=self.region)
        else:
            # Create regular session with access/secret keys
            self.boto3_session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region
            )
            self.glue_client = self.boto3_session.client('glue')

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
            
            # Common wrangler params
            wrangler_params = {
                'sql': sql,
                'database': self.database,
                'ctas_approach': False,
                's3_output': self.s3_output_location,
                'workgroup': self.workgroup,
                'data_source': self.data_source,
            }
            
            # Add optional parameters
            if self.encryption_option:
                wrangler_params['encryption'] = self.encryption_option
            if self.kms_key:
                wrangler_params['kms_key'] = self.kms_key
            if self.result_reuse_enable:
                wrangler_params['cache_seconds'] = self.result_reuse_minutes * 60
            
            # Use session manager if available, otherwise use regular session
            if hasattr(self, 'session_manager'):
                with self.session_manager as session:
                    df = wr.athena.read_sql_query(**wrangler_params, boto3_session=session)
            else:
                df = wr.athena.read_sql_query(**wrangler_params, boto3_session=self.boto3_session)
            
            logger.info("Query executed successfully, returned %d rows", len(df))
            return df
        except Exception as e:
            logger.error("Error executing SQL query: %s", str(e), exc_info=True)
            raise Exception(f"Query execution failed: {str(e)}")

    def test_connection(self) -> dict:
        """
        Test the connection to Athena by running both catalog and query operations.
        Tests specific permissions and access to the configured database.

        Returns:
            dict: Connection test result with success status and message.
        """
        try:
            # First test Glue catalog access
            tables = self.get_tables()
            logger.info(f"Successfully accessed Glue catalog, found {len(tables)} tables")

            # Test database existence and permissions with a more specific query
            test_query = f"""
                SELECT 
                    '{self.database}' as database,
                    current_timestamp as timestamp,
                    'test_connection' as test
                FROM (SELECT 1) dummy
            """
            result = self.execute_query(test_query)
            
            return {
                "success": True,
                "message": (
                    f"Successfully connected to Athena database '{self.database}'. "
                    f"Found {len(tables)} accessible tables."
                )
            }
        except Exception as e:
            if "AccessDenied" in str(e) and "S3" in str(e):
                return {
                    "success": False,
                    "message": f"Connected to Glue catalog but S3 access denied. Check S3 permissions for: {self.s3_output_location}"
                }
            elif "INVALID_INPUT" in str(e) and "database" in str(e).lower():
                return {
                    "success": False,
                    "message": f"Database '{self.database}' does not exist or is not accessible"
                }
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
        return ServiceFormatter(schemas).table_str
