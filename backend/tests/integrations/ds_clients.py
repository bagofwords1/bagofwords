"""
Data Source Integration Tests

Tests connectivity and schema access for data source clients.
Run locally: pytest backend/tests/integrations/ds_clients.py -v
Run specific: pytest backend/tests/integrations/ds_clients.py -k "snowflake" -v
"""
import os
import json
import importlib
import pytest
import logging
from typing import Dict, Any, Generator
from contextlib import contextmanager

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# =============================================================================
# SOURCE OF TRUTH: Data sources to test
# =============================================================================
DATA_SOURCES = [
    "postgresql",
    "mysql",
    "snowflake",
    "bigquery",
    "databricks_sql",
    "powerbi",
]


# =============================================================================
# Container Support (requires Docker + testcontainers)
# =============================================================================
CONTAINER_REGISTRY: Dict[str, Any] = {}

try:
    from testcontainers.postgres import PostgresContainer
    CONTAINER_REGISTRY["postgresql"] = {
        "container_cls": PostgresContainer,
        "image": "postgres:15",
        "get_kwargs": lambda c: {
            "host": c.get_container_host_ip(),
            "port": c.get_exposed_port(5432),
            "database": c.dbname,
            "schema": "public",
            "user": c.username,
            "password": c.password,
        },
        "seed_sql": [
            "CREATE TABLE users (id SERIAL PRIMARY KEY, name VARCHAR(100), email VARCHAR(100))",
            "CREATE TABLE orders (id SERIAL PRIMARY KEY, user_id INT REFERENCES users(id), total DECIMAL(10,2))",
        ],
    }
except ImportError:
    pass

try:
    from testcontainers.mysql import MySqlContainer
    CONTAINER_REGISTRY["mysql"] = {
        "container_cls": MySqlContainer,
        "image": "mysql:8",
        "get_kwargs": lambda c: {
            "host": c.get_container_host_ip(),
            "port": c.get_exposed_port(3306),
            "database": c.dbname,
            "user": c.username,
            "password": c.password,
        },
        "seed_sql": [
            "CREATE TABLE users (id INT AUTO_INCREMENT PRIMARY KEY, name VARCHAR(100), email VARCHAR(100))",
            "CREATE TABLE orders (id INT AUTO_INCREMENT PRIMARY KEY, user_id INT, total DECIMAL(10,2), FOREIGN KEY (user_id) REFERENCES users(id))",
        ],
    }
except ImportError:
    pass


# =============================================================================
# Credentials Loading
# =============================================================================
def load_credentials() -> Dict[str, Any]:
    """Load credentials from integrations.json in the tests folder."""
    credentials_path = os.path.join(os.path.dirname(__file__), "integrations.json")
    if not os.path.exists(credentials_path):
        return {}
    with open(credentials_path, "r") as file:
        return json.load(file)


CREDENTIALS: Dict[str, Any] = load_credentials()
# Support both flat and nested structures
DS_CREDENTIALS: Dict[str, Any] = CREDENTIALS.get("data_sources", CREDENTIALS)


def ds_kwargs(name: str) -> Dict[str, Any]:
    """
    Extract and normalize kwargs for a data source from credentials.
    Skips the test if the data source is missing or disabled.
    """
    cfg = dict(DS_CREDENTIALS.get(name, {}))
    if not cfg:
        pytest.skip(f"{name} missing in integrations.json (data_sources)")
    
    enabled = cfg.pop("enabled", False)
    if not enabled:
        pytest.skip(f"{name} disabled in integrations.json")

    # Remove container flag (handled separately)
    cfg.pop("container", None)

    # Merge common block if provided
    common = cfg.pop("common", {}) or {}
    if isinstance(common, dict):
        cfg.update(common)

    # Prefer nested multi-auth structure: { auth: { type, by_auth: { <type>: {...} } } }
    auth = cfg.pop("auth", None)
    if isinstance(auth, dict):
        auth_type = auth.get("type")
        by_auth = auth.get("by_auth") or {}
        if auth_type and isinstance(by_auth, dict):
            selected = by_auth.get(auth_type, {})
            if isinstance(selected, dict):
                cfg.update(selected)

    # Also support a flat structure: { auth_type: "...", "...": { ... } }
    flat_auth_type = cfg.pop("auth_type", None)
    if flat_auth_type and isinstance(cfg.get(flat_auth_type), dict):
        flat_selected = cfg.pop(flat_auth_type)
        cfg.update(flat_selected)

    return cfg


def is_container_mode(name: str) -> bool:
    """Check if data source is configured for container mode."""
    cfg = DS_CREDENTIALS.get(name, {})
    return cfg.get("container", False) and cfg.get("enabled", False)


@contextmanager
def get_container_kwargs(name: str) -> Generator[Dict[str, Any], None, None]:
    """
    Spin up a container and yield connection kwargs.
    Seeds test data if configured.
    """
    if name not in CONTAINER_REGISTRY:
        pytest.skip(f"No container support for {name} (install testcontainers)")
    
    reg = CONTAINER_REGISTRY[name]
    container_cls = reg["container_cls"]
    image = reg.get("image", "latest")
    
    logger.info(f"{name}: Starting container ({image})...")
    
    with container_cls(image) as container:
        # Seed test data if provided
        seed_sql = reg.get("seed_sql", [])
        if seed_sql:
            import sqlalchemy
            url = container.get_connection_url()
            # Use pymysql driver for MySQL (mysqldb not installed)
            if url.startswith("mysql://"):
                url = url.replace("mysql://", "mysql+pymysql://", 1)
            engine = sqlalchemy.create_engine(url)
            with engine.connect() as conn:
                for sql in seed_sql:
                    conn.execute(sqlalchemy.text(sql))
                conn.commit()
            logger.info(f"{name}: Seeded {len(seed_sql)} tables")
        
        # Get kwargs for our client
        kwargs = reg["get_kwargs"](container)
        yield kwargs


# =============================================================================
# Dynamic Client Factory
# =============================================================================
def get_client(ds_name: str, **kwargs):
    """
    Dynamically import and instantiate a data source client.
    Mirrors the logic in DataSourceService._resolve_client_by_type().
    """
    module_name = f"app.data_sources.clients.{ds_name.lower()}_client"
    # Convert snake_case to TitleCase for class name (e.g., aws_redshift -> AwsRedshift)
    title = "".join(word[:1].upper() + word[1:] for word in ds_name.split("_"))
    class_name = f"{title}Client"
    
    try:
        module = importlib.import_module(module_name)
        ClientClass = getattr(module, class_name)
        return ClientClass(**kwargs)
    except (ImportError, AttributeError) as e:
        raise ValueError(f"Unable to load client for {ds_name}: {e}")


# =============================================================================
# Parametrized Integration Test
# =============================================================================
@pytest.mark.parametrize("ds_name", DATA_SOURCES)
def test_data_source_connection(ds_name: str) -> None:
    """
    Test connectivity and schema access for a data source.
    
    Supports two modes via integrations.json:
    - Remote: "container": false (or omitted) - uses credentials to connect
    - Container: "container": true - spins up Docker container
    
    This mirrors what DataSourceService.test_new_data_source_connection() does:
    1. Test basic connectivity via test_connection()
    2. Validate schema access via get_schemas()
    """
    # Determine mode and get credentials
    if is_container_mode(ds_name):
        # Container mode: spin up Docker and run test inside context
        with get_container_kwargs(ds_name) as cfg:
            _run_connection_test(ds_name, cfg)
    else:
        # Remote mode: use credentials from integrations.json
        cfg = ds_kwargs(ds_name)
        _run_connection_test(ds_name, cfg)


def _run_connection_test(ds_name: str, cfg: Dict[str, Any]) -> None:
    """Run the actual connection + schema test."""
    # Instantiate client
    client = get_client(ds_name, **cfg)
    
    # Step 1: Test connectivity
    logger.info(f"{ds_name}: Testing connection...")
    conn_result = client.test_connection()
    assert conn_result.get("success"), f"{ds_name} connection failed: {conn_result.get('message')}"
    logger.info(f"{ds_name}: Connection successful")
    
    # Step 2: Test schema access
    logger.info(f"{ds_name}: Fetching schemas...")
    schemas = client.get_schemas()
    table_count = len(schemas) if schemas else 0
    logger.info(f"{ds_name}: Found {table_count} tables")
    assert table_count > 0, f"{ds_name}: No tables found. Check schema/dataset name or permissions."
    
    # Log sample tables
    for schema in schemas[:5]:
        table_name = schema.name if hasattr(schema, "name") else schema.get("name", "unknown")
        logger.info(f"{ds_name}: Table: {table_name}")
