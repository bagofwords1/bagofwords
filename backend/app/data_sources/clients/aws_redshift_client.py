from app.data_sources.clients.base import DataSourceClient

import pandas as pd
import psycopg2
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
        password: str = None,
        region: str = None,
        access_key: str = None,
        secret_key: str = None,
        role_arn: str = None,
        cluster_identifier: str = None,
        iam_profile: str = None,
        ssl_mode: str = "require",
        timeout: int = 30
    ):
        logger.info(f"Initializing Redshift client with cluster_identifier: {cluster_identifier}")
        logger.info(f"All parameters: host={host}, port={port}, database={database}, schema={schema}, user={user}, region={region}")
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
        self.access_key = access_key
        self.secret_key = secret_key
        self.role_arn = role_arn
        self.cluster_identifier = cluster_identifier
        self.iam_profile = iam_profile
        self.ssl_mode = self._normalize_ssl_mode(ssl_mode)
        self.timeout = timeout
        self._connection_name = f"redshift_conn_{hash(f'{host}_{port}_{database}_{user}_{schema}')}"
        self._connected = False
        
        # Determine authentication method
        self.auth_method = self._determine_auth_method()
        
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
        """Build the Redshift connection URI (kept for compatibility but not used with psycopg2)."""
        # Handle IAM authentication if specified
        if self.iam_profile or self.auth_method != "PASSWORD":
            # For IAM authentication, we'll use the AWS credentials
            uri = (
                f"postgresql://{self.user}@"
                f"{self.host}:{self.port}/{self.database}"
                f"?sslmode={self.ssl_mode}"
            )
        else:
            # Standard password authentication
            password = self.password or ""
            uri = (
                f"postgresql://{self.user}:{password}@"
                f"{self.host}:{self.port}/{self.database}"
                f"?sslmode={self.ssl_mode}"
            )
        return uri

    def _determine_auth_method(self):
        """Determine the authentication method based on available credentials."""
        if self.role_arn:
            if not self.access_key or not self.secret_key:
                return "ASSUME_ROLE_NO_KEYS"
            else:
                return "ASSUME_ROLE_KEYS"
        elif self.access_key and self.secret_key:
            return "KEYS"
        elif not self.password and self.cluster_identifier:
            return "ROLE"
        else:
            return "PASSWORD"

    @staticmethod
    def _normalize_ssl_mode(value) -> str:
        """Normalize ssl_mode to a valid psycopg2 value.

        Accepts booleans and common string synonyms and returns one of:
        disable, allow, prefer, require, verify-ca, verify-full
        """
        valid = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}

        if isinstance(value, bool):
            return "require" if value else "disable"
        if value is None:
            return "require"

        s = str(value).strip().lower()
        if s in ("true", "1", "yes", "on"):
            return "require"
        if s in ("false", "0", "no", "off"):
            return "disable"

        synonyms = {
            "required": "require",
            "verify_ca": "verify-ca",
            "verify_full": "verify-full",
        }
        s = synonyms.get(s, s)
        return s if s in valid else "require"

    def _get_iam_credentials(self):
        """Get IAM credentials for Redshift authentication."""
        logger.info(f"Auth method: {self.auth_method}")
        logger.info(f"Cluster identifier: {self.cluster_identifier}")
        
        # Create the appropriate session based on authentication method
        if self.auth_method in ("ASSUME_ROLE_KEYS", "ASSUME_ROLE_NO_KEYS"):
            if self.auth_method == "ASSUME_ROLE_KEYS":
                assume_client = boto3.client(
                    "sts",
                    region_name=self.region,
                    aws_access_key_id=self.access_key,
                    aws_secret_access_key=self.secret_key,
                )
            else:
                assume_client = boto3.client("sts", region_name=self.region)
            
            role_session = f"bagofwords_{hash(f'{self.host}_{self.port}_{self.database}_{self.user}')}"
            session_keys = assume_client.assume_role(
                RoleArn=self.role_arn, RoleSessionName=role_session
            )["Credentials"]
            
            session = boto3.Session(
                aws_access_key_id=session_keys["AccessKeyId"],
                aws_secret_access_key=session_keys["SecretAccessKey"],
                aws_session_token=session_keys["SessionToken"],
                region_name=self.region
            )
        else:
            session = self.session_manager.session if self.role_arn else self.boto3_session
        
        # Use redshift client for provisioned clusters
        if not self.cluster_identifier:
            raise ValueError("cluster_identifier is required for Redshift")
        logger.info(f"Using Redshift cluster: {self.cluster_identifier}")
        client = session.client("redshift", region_name=self.region)
        
        credentials = client.get_cluster_credentials(
            DbUser=self.user,
            DbName=self.database,
            ClusterIdentifier=self.cluster_identifier,
        )
        
        return credentials["DbUser"], credentials["DbPassword"]

    @contextmanager
    def connect(self) -> Generator[psycopg2.extensions.connection, None, None]:
        """Yield a connection to a Redshift database using raw psycopg2."""
        conn = None
        try:
            logger.info(f"Attempting to connect to Redshift with auth_method: {self.auth_method}")
            
            # Get connection parameters
            if self.auth_method != "PASSWORD":
                if not self.cluster_identifier:
                    raise ValueError("cluster_identifier is required for IAM authentication")
                logger.info("Getting IAM credentials...")
                db_user, db_password = self._get_iam_credentials()
                logger.info(f"Using IAM authentication with user: {db_user}")
            else:
                db_user = self.user
                db_password = self.password
                logger.info(f"Using password authentication with user: {db_user}")
            
            # Import psycopg2
            import psycopg2
            
            logger.info(f"Creating psycopg2 connection with timeout: {self.timeout}")
            # Create raw psycopg2 connection
            conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=db_user,
                password=db_password,
                connect_timeout=self.timeout,
                application_name="bagofwords_redshift_client",
                sslmode=self.ssl_mode
            )
            
            logger.info("Connection established successfully!")
            
            # Set the search path to the specified schema
            if self.schema:
                logger.info(f"Setting search_path to: {self.schema}")
                with conn.cursor() as cursor:
                    cursor.execute(f"SET search_path TO {self.schema}")
                    conn.commit()
            
            yield conn
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            raise RuntimeError(f"Error connecting to Redshift: {e}")
        finally:
            if conn is not None:
                conn.close()

    def execute_query(self, sql: str) -> pd.DataFrame:
        """Execute SQL statement and return the result as a DataFrame."""
        try:
            with self.connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql)
                    # Get column names
                    columns = [desc[0] for desc in cursor.description]
                    # Fetch all rows
                    rows = cursor.fetchall()
                    # Create DataFrame
                    df = pd.DataFrame(rows, columns=columns)
            return df
        except Exception as e:
            logger.error(f"Error executing SQL: {e}")
            raise

    def get_tables(self) -> List[Table]:
        """Get tables with graceful fallback if enriched query fails."""
        try:
            return self._get_tables_enriched()
        except Exception:
            return self._get_tables_basic()

    def _normalize_data_type(self, data_type: str) -> str:
        """Normalize Redshift-specific data types."""
        if data_type == 'character varying':
            return "varchar"
        elif data_type == 'numeric':
            return "numeric"
        elif data_type == 'double precision':
            return "double precision"
        elif data_type == 'timestamp without time zone':
            return "timestamp"
        elif data_type == 'timestamp with time zone':
            return "timestamptz"
        return data_type

    def _get_tables_enriched(self) -> List[Table]:
        """Get tables with column/table comments. May fail on some Redshift configurations."""
        logger.info(f"_get_tables_enriched() called with schema: {self.schema}")
        with self.connect() as conn:
            with conn.cursor() as cursor:
                # Query with JOINs to pg_description for comments (like PostgreSQL)
                sql = """
                    SELECT
                        t.table_name,
                        c.column_name,
                        c.data_type,
                        c.ordinal_position,
                        pd_col.description AS column_comment,
                        pd_tbl.description AS table_comment
                    FROM information_schema.tables t
                    JOIN information_schema.columns c
                        ON t.table_name = c.table_name
                        AND t.table_schema = c.table_schema
                    LEFT JOIN pg_catalog.pg_class pc
                        ON pc.relname = t.table_name
                        AND pc.relnamespace = (SELECT oid FROM pg_catalog.pg_namespace WHERE nspname = t.table_schema)
                    LEFT JOIN pg_catalog.pg_description pd_col
                        ON pd_col.objoid = pc.oid
                        AND pd_col.objsubid = c.ordinal_position
                    LEFT JOIN pg_catalog.pg_description pd_tbl
                        ON pd_tbl.objoid = pc.oid
                        AND pd_tbl.objsubid = 0
                    WHERE t.table_schema = %s
                    AND t.table_type = 'BASE TABLE'
                    ORDER BY t.table_name, c.ordinal_position
                """

                cursor.execute(sql, (self.schema,))
                rows = cursor.fetchall()
                logger.info(f"Enriched query returned {len(rows)} rows for schema '{self.schema}'")

                tables = {}
                for row in rows:
                    table_name, column_name, data_type, ordinal_position, col_comment, tbl_comment = row
                    full_data_type = self._normalize_data_type(data_type)

                    if table_name not in tables:
                        tables[table_name] = Table(
                            name=table_name,
                            description=tbl_comment if tbl_comment else None,
                            columns=[],
                            pks=[],
                            fks=[],
                            metadata_json={"schema": self.schema}
                        )
                    tables[table_name].columns.append(TableColumn(
                        name=column_name,
                        dtype=full_data_type,
                        description=col_comment if col_comment else None
                    ))

                logger.info(f"Found {len(tables)} tables in schema '{self.schema}'")
                return list(tables.values())

    def _get_tables_basic(self) -> List[Table]:
        """Get tables without comments (original query - always works)."""
        logger.info(f"_get_tables_basic() called with schema: {self.schema}")
        try:
            with self.connect() as conn:
                with conn.cursor() as cursor:
                    sql = """
                        SELECT
                            t.table_name,
                            c.column_name,
                            c.data_type,
                            c.ordinal_position
                        FROM information_schema.tables t
                        JOIN information_schema.columns c
                            ON t.table_name = c.table_name
                            AND t.table_schema = c.table_schema
                        WHERE t.table_schema = %s
                        AND t.table_type = 'BASE TABLE'
                        ORDER BY t.table_name, c.ordinal_position
                    """

                    cursor.execute(sql, (self.schema,))
                    rows = cursor.fetchall()
                    logger.info(f"Basic query returned {len(rows)} rows for schema '{self.schema}'")

                    # If no tables found, try a fallback query to see what schemas exist
                    if not rows:
                        logger.warning(f"No tables found in schema '{self.schema}', checking available schemas...")
                        cursor.execute("""
                            SELECT DISTINCT table_schema
                            FROM information_schema.tables
                            WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'pg_internal')
                            ORDER BY table_schema
                        """)
                        available_schemas = [row[0] for row in cursor.fetchall()]
                        logger.info(f"Available schemas: {available_schemas}")

                        # Try the 'public' schema if current schema is empty
                        if self.schema != 'public' and 'public' in available_schemas:
                            logger.info("Trying 'public' schema as fallback...")
                            cursor.execute(sql, ('public',))
                            rows = cursor.fetchall()
                            logger.info(f"Fallback query returned {len(rows)} rows for 'public' schema")

                    tables = {}
                    for row in rows:
                        table_name, column_name, data_type, ordinal_position = row
                        full_data_type = self._normalize_data_type(data_type)

                        if table_name not in tables:
                            tables[table_name] = Table(
                                name=table_name,
                                columns=[],
                                pks=[],
                                fks=[],
                                metadata_json={"schema": self.schema}
                            )
                        tables[table_name].columns.append(TableColumn(name=column_name, dtype=full_data_type))

                    logger.info(f"Found {len(tables)} tables in schema '{self.schema}'")
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
            # Use raw psycopg2 connection to avoid SQLAlchemy's PostgreSQL-specific queries
            import psycopg2
            
            # Get connection parameters
            if self.auth_method != "PASSWORD":
                if not self.cluster_identifier:
                    raise ValueError("cluster_identifier is required for IAM authentication")
                db_user, db_password = self._get_iam_credentials()
            else:
                db_user = self.user
                db_password = self.password
            
            # Create raw psycopg2 connection
            conn = psycopg2.connect(
                host=self.host,
                port=self.port,
                database=self.database,
                user=db_user,
                password=db_password,
                connect_timeout=self.timeout,
                application_name="bagofwords_redshift_client",
                sslmode=self.ssl_mode
            )
            
            # Test with a simple query
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1 as test")
                test_result = cursor.fetchone()
                
                # Also check if there are any tables in the schema
                cursor.execute("""
                    SELECT COUNT(*) 
                    FROM information_schema.tables 
                    WHERE table_schema = %s AND table_type = 'BASE TABLE'
                """, (self.schema,))
                table_count = cursor.fetchone()[0]
                
                # Check available schemas
                cursor.execute("""
                    SELECT DISTINCT table_schema 
                    FROM information_schema.tables 
                    WHERE table_schema NOT IN ('information_schema', 'pg_catalog', 'pg_internal')
                    ORDER BY table_schema
                """)
                available_schemas = [row[0] for row in cursor.fetchall()]
            
            conn.close()
            
            if test_result and test_result[0] == 1:
                message = f"Successfully connected to Redshift database '{self.database}' (schema: {self.schema})"
                if table_count == 0:
                    message += f" - Warning: No tables found in schema '{self.schema}'"
                    message += f" - Available schemas: {available_schemas}"
                else:
                    message += f" - Found {table_count} tables in schema '{self.schema}'"
                
                return {
                    "success": True,
                    "message": message
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
        description = f"AWS Redshift (provisioned) database at {self.host}:{self.port}/{self.database} (schema: {self.schema})\n\n"
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