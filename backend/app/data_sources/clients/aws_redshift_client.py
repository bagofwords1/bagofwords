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
            RoleSessionName="RedshiftClientSession",
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

class AwsRedshiftClient(DataSourceClient):
    def __init__(
        self,
        host: str,
        port: int,
        database: str,
        schema: str,
        user: str,
        password: str,
        region: str = None,
        access_key: str = None,
        secret_key: str = None,
        role_arn: str = None,
        cluster_identifier: str = None,
        iam_profile: str = None,
        ssl_mode: str = "require",
        timeout: int = 30
    ):
        """
        Initialize the Redshift client.

        Args:
            host (str): Redshift cluster endpoint
            port (int): Redshift port (default: 5439)
            database (str): Database name
            schema (str): Schema name
            user (str): Username
            password (str): Password
            region (str, optional): AWS region
            access_key (str, optional): AWS access key ID
            secret_key (str, optional): AWS secret access key
            role_arn (str, optional): AWS IAM Role ARN to assume
            cluster_identifier (str, optional): Redshift cluster identifier
            iam_profile (str, optional): IAM profile for authentication
            ssl_mode (str): SSL mode for connection
            timeout (int): Connection timeout in seconds
        """
        self.host = host
        self.port = port
        self.database = database
        self.schema = schema
        self.user = user
        self.password = password
        self.region = region
        self.cluster_identifier = cluster_identifier
        self.iam_profile = iam_profile
        self.ssl_mode = ssl_mode
        self.timeout = timeout
        self._connection_name = f"redshift_conn_{hash(f'{host}_{port}_{database}_{user}_{schema}')}"
        self._connected = False
        
        if role_arn:
            self.session_manager = SessionManager(role_arn, region)
        else:
            # Create regular session with access/secret keys
            self.boto3_session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region
            ) if access_key and secret_key else None

    @cached_property
    def redshift_uri(self):
        """Build the Redshift connection URI."""
        # Handle IAM authentication if specified
        if self.iam_profile:
            # For IAM authentication, we'll use the AWS credentials
            uri = (
                f"postgresql://{self.user}@"
                f"{self.host}:{self.port}/{self.database}"
                f"?sslmode={self.ssl_mode}"
            )
        else:
            # Standard password authentication
            uri = (
                f"postgresql://{self.user}:{self.password}@"
                f"{self.host}:{self.port}/{self.database}"
                f"?sslmode={self.ssl_mode}"
            )
        return uri

    @contextmanager
    def connect(self) -> Generator[sqlalchemy.engine.base.Connection, None, None]:
        """Yield a connection to a Redshift database."""
        engine = None
        conn = None
        try:
            # Create engine with appropriate configuration
            engine = sqlalchemy.create_engine(
                self.redshift_uri,
                connect_args={
                    "connect_timeout": self.timeout,
                    "application_name": "bagofwords_redshift_client"
                }
            )
            conn = engine.connect()
            
            # Set the search path to the specified schema
            if self.schema:
                conn.execute(text(f"SET search_path TO {self.schema}"))
            
            yield conn
        except Exception as e:
            raise RuntimeError(f"Error connecting to Redshift: {e}")
        finally:
            if conn is not None:
                conn.close()
            if engine is not None:
                engine.dispose()

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        try:
            with self.connect() as conn:
                df = pd.read_sql(text(sql), conn)
            return df
        except Exception as e:
            logger.error(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Get all tables and their columns in the specified schema."""
        try:
            with self.connect() as conn:
                # Query to get table and column information from Redshift system tables
                sql = """
                    SELECT 
                        t.table_name,
                        c.column_name,
                        c.data_type,
                        c.character_maximum_length,
                        c.numeric_precision,
                        c.numeric_scale,
                        c.is_nullable
                    FROM information_schema.tables t
                    JOIN information_schema.columns c 
                        ON t.table_name = c.table_name 
                        AND t.table_schema = c.table_schema
                    WHERE t.table_schema = :schema
                        AND t.table_type = 'BASE TABLE'
                    ORDER BY t.table_name, c.ordinal_position
                """
                
                result = conn.execute(text(sql), {"schema": self.schema})
                rows = result.fetchall()

                tables = {}
                for row in rows:
                    table_name, column_name, data_type, char_length, num_precision, num_scale, is_nullable = row
                    
                    # Build complete data type
                    if data_type == 'character varying' and char_length:
                        full_data_type = f"varchar({char_length})"
                    elif data_type == 'numeric' and num_precision and num_scale:
                        full_data_type = f"numeric({num_precision},{num_scale})"
                    else:
                        full_data_type = data_type

                    if table_name not in tables:
                        tables[table_name] = Table(
                            name=table_name, columns=[], pks=[], fks=[])
                    tables[table_name].columns.append(
                        TableColumn(name=column_name, dtype=full_data_type))
                
                return list(tables.values())
        except Exception as e:
            logger.error(f"Error retrieving tables: {e}")
            return []

    def get_schema(self, table_id: str) -> Table:
        """This method is now obsolete. Please use get_tables() instead."""
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead.")

    def get_schemas(self):
        """Get schemas for all tables in the specified database."""
        return self.get_tables()

    def prompt_schema(self):
        schemas = self.get_schemas()
        return TableFormatter(schemas).table_str

    def test_connection(self):
        """Test connection to Redshift and return status information."""
        try:
            with self.connect() as conn:
                # Test with a simple query
                result = conn.execute(text("SELECT 1 as test"))
                test_result = result.fetchone()
                
                if test_result and test_result[0] == 1:
                    return {
                        "success": True,
                        "message": f"Successfully connected to Redshift database '{self.database}' (schema: {self.schema})"
                    }
                else:
                    return {
                        "success": False,
                        "message": "Connection test failed - no result returned"
                    }
        except Exception as e:
            return {
                "success": False,
                "message": str(e)
            }

    @property
    def description(self):
        system_prompt = """
        You can call the execute_query method to run SQL queries on Redshift.
        
        The below are examples for how to use the execute_query method. Note that the actual SQL will vary based on the schema.
        Notice only the SQL syntax and instructions on how to use the execute_query method, not the actual SQL queries.

        ```python
        df = client.execute_query("SELECT * FROM users")
        ```
        or:
        ```python
        df = client.execute_query("SELECT * FROM users WHERE age > 30")
        ```

        Redshift is a fully managed, petabyte-scale data warehouse service in the cloud.
        """
        description = f"AWS Redshift database at {self.host}:{self.port}/{self.database} (schema: {self.schema})\n\n"
        description += system_prompt

        return description

    def __del__(self):
        """Clean up connection when object is destroyed."""
        if self._connected:
            try:
                # SQLAlchemy manages connections internally
                pass
            except:
                pass 