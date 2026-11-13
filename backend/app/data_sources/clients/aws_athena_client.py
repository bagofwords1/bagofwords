from app.data_sources.clients.progress import (
    discovery_progress, discovery_items, discovery_phase, IndexingCancelled,
    NOT_FOUND_REASON, SKIPPED_NAME_REASON, format_name_list, unreadable_note,
)
from app.data_sources.clients.base import DataSourceClient
import pandas as pd
from typing import List, Optional
from app.ai.prompt_formatters import Table, TableColumn, ServiceFormatter
import logging
import awswrangler as wr
import boto3
from tenacity import retry, stop_after_attempt, wait_fixed
import threading
from datetime import datetime, timedelta, timezone
import os
import fnmatch
from botocore.config import Config as BotoConfig
from app.utils.safe_regex import compile_bounded, pattern_cost_problem
from app.schemas.data_sources.configs import parse_athena_database_spec as parse_database_spec

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

    def __init__(self, role_arn: str, region: str, access_key: str = None, secret_key: str = None) -> None:
        if hasattr(self, '_initialized') and self._initialized:
            return
        self.role_arn = role_arn
        self.region = region
        self.access_key = access_key
        self.secret_key = secret_key
        self._renew_session()
        self._initialized = True

    def _renew_session(self) -> None:
        """Assume the role and create a new session."""
        logger.info("Renewing AWS session.")
        if self.access_key and self.secret_key:
            sts_client = boto3.client(
                "sts",
                region_name=self.region,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
            )
        else:
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

_GLOB_CHARS = "*?["
# Skipped dotted table names are listed one by one up to this many per crawl;
# the rest are rolled into a single "+N more" entry, so a database full of
# dotted names cannot bloat the indexing job's stats.
MAX_SKIPPED_TABLE_ENTRIES = 50


class ExcludePatternTimeout(RuntimeError):
    pass


def _is_glob(entry: str) -> bool:
    return any(c in entry for c in _GLOB_CHARS)


def _error_code(exc: Exception) -> Optional[str]:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return (response.get("Error") or {}).get("Code")
    return None


def _failure_reason(exc: Exception) -> str:
    return _error_code(exc) or type(exc).__name__


class AwsAthenaClient(DataSourceClient):
    unindexed_table_prefix_separator = "."

    # Athena runs queries on its Trino engine: '' only (sqlglot trino).
    sql_literal_style = "standard"

    # Rendered into codegen prompts (<connection_clients>) so generated queries
    # express time windows with the engine's own relative date functions instead
    # of literal dates that go stale when saved code is re-executed.
    relative_date_hint = "Relative dates (Athena/Presto SQL): current_date, current_date - interval '7' day, date_trunc('month', current_date); the clock is UTC."

    def __init__(
        self,
        region: str,
        database: str,
        s3_output_location: str = None,
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
        kms_key: str = None,
        table_exclude_pattern: Optional[str] = None,
    ):
        """
        Initialize the Athena client using AWS Wrangler.

        Args:
            region (str): AWS region
            database (str): One Glue database name, or comma-separated names, globs
                and `!`-negations (e.g. `*_db, !billing_db`) to index several
            table_exclude_pattern (str, optional): Regex; tables whose bare name matches are skipped
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
        self.table_exclude_pattern = (table_exclude_pattern or "").strip() or None
        if self.table_exclude_pattern and (
            pattern_cost_problem(self.table_exclude_pattern)
            or compile_bounded(self.table_exclude_pattern) is None
        ):
            raise ValueError("Invalid table_exclude_pattern")
        self._includes, self._excludes, self._multi = parse_database_spec(database)
        self.exact_table_scope_catalogs = ("awsdatacatalog",) if self._multi else None
        self._unread_dbs: List[str] = []
        self._diagnostics: List[dict] = []
        self._attached_dbs: List[str] = []
        self._count_cache: Optional[tuple] = None
        self._skipped_tables = 0
        self._counted_dbs = 0

        glue_config = BotoConfig(retries={"mode": "adaptive", "max_attempts": 10})
        if role_arn:
            self.session_manager = SessionManager(role_arn, region, access_key, secret_key)
            # Initialize session for Glue client
            with self.session_manager as session:
                self.glue_client = session.client('glue', region_name=self.region, config=glue_config)
        else:
            # Create regular session with access/secret keys
            self.boto3_session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=region
            )
            self.glue_client = self.boto3_session.client('glue', config=glue_config)

    def execute_query(self, sql: str) -> pd.DataFrame:
        """
        Execute an SQL query and return the result as a DataFrame using AWS Wrangler.

        Args:
            sql (str): The SQL query to execute.

        Returns:
            pd.DataFrame: Query result as a Pandas DataFrame.
        """
        if self._multi:
            self._check_multi_table_refs(sql)
        try:
            logger.info("Executing query: %s", sql)
            
            # Common wrangler params
            wrangler_params = {
                'sql': sql,
                'database': None if self._multi else self.database,
                'ctas_approach': False,
                'workgroup': self.workgroup,
                'data_source': self.data_source,
            }
            if self.s3_output_location:
                wrangler_params['s3_output'] = self.s3_output_location
            
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
        Tests specific permissions and access to the configured database(s).

        Returns:
            dict: Connection test result with success status and message.
        """
        try:
            table_count = self.count_tables()
            logger.info(f"Successfully accessed Glue catalog, found {table_count} tables")

            self.execute_query("SELECT 1")

            if not self._multi:
                message = (
                    f"Successfully connected to Athena database '{self.database}'. "
                    f"Found {table_count} accessible tables."
                )
            else:
                message = (
                    f"Successfully connected to Athena: {self._counted_dbs} databases, "
                    f"{table_count} tables."
                )
            message += unreadable_note(self._reported_diagnostics())
            return {"success": True, "message": message}
        except Exception as e:
            if "AccessDenied" in str(e) and "S3" in str(e):
                location = self.s3_output_location or "workgroup output location"
                return {
                    "success": False,
                    "message": f"Connected to Glue catalog but S3 access denied. Check S3 permissions for: {location}"
                }
            elif not self._multi and (
                "EntityNotFoundException" in str(e)
                or ("INVALID_INPUT" in str(e) and "database" in str(e).lower())
            ):
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
        quoting = (
            "Double-quote any database or table name that is not made of lowercase letters, "
            'digits and underscores starting with a letter or underscore, e.g. "globex_db"."my-table".\n'
        )
        if not self._multi:
            system_prompt = f"""
        You can call the execute_query method to run SQL queries.

        Examples:
        ```python
        # Query specific table
        df = client.execute_query("SELECT * FROM my_table LIMIT 10")

        # Inspect a table's columns
        df = client.execute_query("SELECT * FROM my_table LIMIT 1")
        ```
        {quoting}"""
            return f"AWS Athena database: {self.database}\n\n" + system_prompt

        dbs = sorted(self._attached_dbs)
        listed = (
            f"({len(dbs)}): {format_name_list(dbs, 50)}" if dbs else "(none indexed yet)"
        )
        return (
            f"AWS Athena databases {listed}\n\n"
            "You can call the execute_query method to run SQL queries.\n"
            "Every table is named <database>.<table> (for example globex_db.assets). There is no "
            "default database: queries that name a table without its database are refused. "
            "SHOW and DESCRIBE are not available.\n"
            f"{quoting}"
            "To list one database's tables, use the describe_tables tool with the pattern ^<database>\\.\n\n"
            "Examples:\n"
            "```python\n"
            'df = client.execute_query("SELECT * FROM globex_db.assets LIMIT 10")\n'
            "```\n"
        )

    def _check_multi_table_refs(self, sql: str) -> None:
        """Refuse what multi mode cannot run safely, before Athena sees it.

        This holds even when no agent table scope is attached (a source not
        indexed yet): a table must be `<database>.<table>`, in a database the
        `database` setting includes, behind no catalog but this connection's.
        """
        from app.ai.code_execution.table_scope import table_refs
        refs = table_refs(sql, "athena")
        unqualified = [r[-1] for r in refs if len(r) == 1]
        if unqualified:
            raise ValueError(
                f"Unqualified table reference(s): {', '.join(dict.fromkeys(unqualified))}. This "
                "connection has no default database; name every table as <database>.<table>."
            )
        catalog = (self.data_source or "").lower()
        outside = [
            ".".join(r) for r in refs
            if (len(r) == 3 and r[0] != catalog) or not self._spec_matches(r[-2])
        ]
        if outside:
            raise ValueError(
                f"Table(s) outside this connection's databases: {', '.join(dict.fromkeys(outside))}. "
                "Only the databases in the connection's Database setting can be queried."
            )

    def _spec_matches(self, database: str) -> bool:
        name = database.lower()
        included = any(
            fnmatch.fnmatchcase(name, i.lower()) if _is_glob(i) else name == i.lower()
            for i in self._includes
        )
        return included and not any(fnmatch.fnmatchcase(name, e.lower()) for e in self._excludes)

    def _resolve_databases(self) -> List[str]:
        """A failed listing page raises: a partial list would make unread databases look gone."""
        available: List[str] = []
        if any(_is_glob(i) for i in self._includes):
            paginator = self.glue_client.get_paginator('get_databases')
            for page in paginator.paginate():
                available.extend(str(d['Name']).lower() for d in page['DatabaseList'])
        resolved: List[str] = []
        for entry in self._includes:
            if _is_glob(entry):
                pattern = entry.lower()
                resolved.extend(n for n in available if fnmatch.fnmatchcase(n, pattern))
            else:
                resolved.append(entry.lower())
        resolved = [
            n for n in resolved
            if not any(fnmatch.fnmatchcase(n, e.lower()) for e in self._excludes)
        ]
        databases: List[str] = []
        for name in dict.fromkeys(resolved):
            if "." in name:
                self._diagnostics.append({"name": name, "reason": SKIPPED_NAME_REASON})
            else:
                databases.append(name)
        return databases

    def _exclude_matcher(self):
        if not self.table_exclude_pattern:
            return None
        return compile_bounded(self.table_exclude_pattern)

    def _glue_tables(self, database: str, matcher):
        paginator = self.glue_client.get_paginator('get_tables')
        prefix = f"{database}." if self._multi else ""
        for page in discovery_items(paginator.paginate(DatabaseName=database), 'catalog_pages'):
            # Filter first, so indexing progress counts only tables that get indexed.
            kept = [t for t in page['TableList'] if self._keep_table(database, t['Name'], matcher)]
            yield from discovery_items(kept, 'tables', label=lambda table: f"{prefix}{table['Name']}")

    def _keep_table(self, database: str, name: str, matcher) -> bool:
        if self._multi and "." in name:
            self._skip_dotted_table(f"{database}.{name}")
            return False
        if matcher is None:
            return True
        excluded = matcher.search(name)
        if matcher.dead:
            raise ExcludePatternTimeout(
                "table_exclude_pattern timed out; simplify the regular expression"
            )
        return not excluded

    def _skip_dotted_table(self, full_name: str) -> None:
        self._skipped_tables += 1
        if self._skipped_tables <= MAX_SKIPPED_TABLE_ENTRIES:
            self._diagnostics.append({"name": full_name, "reason": SKIPPED_NAME_REASON})

    def _reported_diagnostics(self) -> List[dict]:
        extra = self._skipped_tables - MAX_SKIPPED_TABLE_ENTRIES
        if extra <= 0:
            return list(self._diagnostics)
        return [*self._diagnostics, {"name": f"+{extra} more tables", "reason": SKIPPED_NAME_REASON}]

    def _reset_run_state(self) -> None:
        self._unread_dbs = []
        self._diagnostics = []
        self._skipped_tables = 0

    def _record_database_failure(self, database: str, exc: Exception) -> None:
        if _error_code(exc) == "EntityNotFoundException":
            self._diagnostics.append({"name": database, "reason": NOT_FOUND_REASON})
            return
        logger.warning("Could not read Glue database %s: %s", database, exc)
        self._unread_dbs.append(database)
        self._diagnostics.append({"name": database, "reason": _failure_reason(exc)})

    def _build_table(self, database: str, table: dict) -> Table:
        columns = [
            TableColumn(name=col['Name'], dtype=col['Type'])
            for col in table.get('StorageDescriptor', {}).get('Columns', [])
        ]
        columns += [
            TableColumn(name=p['Name'], dtype=p['Type']) for p in table.get('PartitionKeys', [])
        ]
        if not self._multi:
            return Table(name=table['Name'], columns=columns, pks=None, fks=None)
        return Table(
            name=f"{database}.{table['Name']}",
            columns=columns,
            pks=None,
            fks=None,
            metadata_json={"database": database},
        )

    def get_tables(self) -> List[Table]:
        self._reset_run_state()
        self._count_cache = None
        try:
            matcher = self._exclude_matcher()
            if not self._multi:
                databases = [self.database]
            else:
                databases = self._resolve_databases()
                discovery_phase('databases')
                databases = discovery_items(databases, 'databases', label=lambda d: d)

            tables = {}
            for database in databases:
                found = []
                try:
                    for table in self._glue_tables(database, matcher):
                        found.append(self._build_table(database, table))
                except (IndexingCancelled, ExcludePatternTimeout):
                    raise
                except Exception as e:
                    if not self._multi:
                        raise
                    self._record_database_failure(database, e)
                    continue
                for built in found:
                    tables[built.name] = built

            return list(tables.values())
            
        except IndexingCancelled:
            raise
        except ExcludePatternTimeout as e:
            # The refresh keeps the previous catalog and shows this reason.
            self.mark_discovery_incomplete(str(e))
            logger.error("Error retrieving tables: %s", e)
            return []
        except Exception as e:
            self.mark_discovery_incomplete(
                "Glue database listing failed" if self._multi else "aws_athena catalog"
            )
            logger.error(f"Error retrieving tables: {e}")
            return []

    def count_tables(self) -> int:
        """One failed database is recorded in `index_stats`; raises only when none can be read."""
        if self._count_cache is not None:
            total, self._counted_dbs, diagnostics, unread, skipped = self._count_cache
            self._diagnostics, self._unread_dbs, self._skipped_tables = list(diagnostics), list(unread), skipped
            return total
        self._reset_run_state()
        matcher = self._exclude_matcher()
        databases = self._resolve_databases() if self._multi else [self.database]
        if not databases:
            raise RuntimeError(f"No Glue databases match '{self.database}'")
        total = 0
        readable = 0
        for database in databases:
            try:
                count = sum(1 for _ in self._glue_tables(database, matcher))
            except (IndexingCancelled, ExcludePatternTimeout):
                raise
            except Exception as e:
                if not self._multi:
                    raise
                self._record_database_failure(database, e)
                continue
            total += count
            readable += 1
        if readable == 0:
            reasons = "; ".join(f"{d['name']}: {d['reason']}" for d in self._diagnostics[:5])
            raise RuntimeError(f"None of the Glue databases could be read. {reasons}")
        self._counted_dbs = readable
        self._count_cache = (
            total, readable, list(self._diagnostics), list(self._unread_dbs), self._skipped_tables,
        )
        return total

    def unindexed_table_prefixes(self) -> List[str]:
        return list(self._unread_dbs)

    def index_stats(self) -> dict:
        diagnostics = self._reported_diagnostics()
        if not diagnostics:
            return {}
        return {
            "unreadable_datasets": diagnostics,
            "unreadable_dataset_count": len(diagnostics),
        }

    def attach_table_metadata(self, tables: List[dict]) -> None:
        if not self._multi:
            return
        learned = set(self._attached_dbs)
        for t in tables or []:
            meta = (t.get("metadata_json") if isinstance(t, dict) else None) or {}
            db = meta.get("database") if isinstance(meta, dict) else None
            if isinstance(db, str) and db and self._spec_matches(db):
                learned.add(db.lower())
        self._attached_dbs = sorted(learned)

    def get_schema(self, table_id: str) -> Table:
        """
        Retrieve metadata for a specific table.
        This method is now obsolete and should not be used.
        """
        raise NotImplementedError(
            "get_schema() is obsolete. Use get_tables() instead."
        )

    @discovery_progress
    def get_schemas(self, progress_callback=None):
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
