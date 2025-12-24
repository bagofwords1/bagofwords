import asyncio
import pytest
import os
import sys
import atexit
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Generator, AsyncGenerator
from fastapi.testclient import TestClient
from alembic.config import Config
from alembic import command

# ============================================================================
# PostgreSQL Container Support - MUST run before app imports
# ============================================================================
_postgres_container = None


def _get_db_backend_from_argv():
    """Parse --db option from sys.argv before pytest processes it."""
    for i, arg in enumerate(sys.argv):
        if arg == "--db" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--db="):
            return arg.split("=", 1)[1]
    return os.environ.get("TEST_DB", "sqlite")


def _setup_test_database():
    """Setup test database - called at module load time, before app imports."""
    global _postgres_container
    
    db_backend = _get_db_backend_from_argv()
    print(f"\n📊 Test database backend: {db_backend}")
    
    if db_backend == "postgres":
        from testcontainers.postgres import PostgresContainer
        
        print("🐘 Starting PostgreSQL container...")
        _postgres_container = PostgresContainer("postgres:15")
        _postgres_container.start()
        
        # Get connection URL and set as environment variable BEFORE settings loads
        sync_url = _postgres_container.get_connection_url()
        # testcontainers returns postgresql+psycopg2:// URL
        clean_url = sync_url.replace("postgresql+psycopg2://", "postgresql://")
        
        os.environ["TEST_DATABASE_URL"] = clean_url
        print(f"✅ PostgreSQL container ready: {clean_url.split('@')[1] if '@' in clean_url else clean_url}")
        
        # Register cleanup on exit
        atexit.register(_cleanup_container)
    else:
        # SQLite - set URL with process ID and UUID for isolation (prevents CI race conditions)
        os.environ["TEST_DATABASE_URL"] = f"sqlite:///db/test_{os.getpid()}_{uuid.uuid4().hex[:8]}.db"


def _cleanup_container():
    """Cleanup PostgreSQL container."""
    global _postgres_container
    if _postgres_container is not None:
        print("\n🛑 Stopping PostgreSQL container...")
        _postgres_container.stop()
        _postgres_container = None


# >>> CRITICAL: Setup database BEFORE importing app modules <<<
_setup_test_database()

# Now it's safe to import app modules (env var is set)
from app.models.base import Base
from app.settings.config import settings
from app.settings.database import create_async_database_engine, create_async_session_factory

# Ensure the application uses the test database/engine during tests
settings.TESTING = True


def pytest_addoption(parser):
    """Add --db CLI option for selecting test database backend."""
    parser.addoption(
        "--db",
        action="store",
        default="sqlite",
        choices=["sqlite", "postgres"],
        help="Database backend for tests: sqlite (default, fast) or postgres (thorough)"
    )


def pytest_configure(config):
    """Configure pytest markers."""
    config.addinivalue_line("markers", "e2e: marks tests as end-to-end tests")

@pytest.fixture(scope="session", autouse=True)
def disable_telemetry_for_tests():
    """Disable telemetry during the entire pytest session via BowConfig only."""
    settings.bow_config.telemetry.enabled = False

from tests.fixtures.client import test_client
from tests.fixtures.user import create_user
from tests.fixtures.auth import login_user, whoami
from tests.fixtures.organization import create_organization, add_organization_member, get_organization_members, update_organization_member, remove_organization_member, get_user_organizations
from tests.fixtures.llm import create_llm_provider_and_models, get_models, get_default_model, set_llm_provider_as_default, toggle_llm_active_status, delete_llm_provider, create_openai_provider_with_base_url, update_llm_provider_base_url, create_azure_provider_and_models
from tests.fixtures.report import create_report, get_reports, get_report, update_report, delete_report, publish_report, rerun_report, schedule_report, get_public_report
from tests.fixtures.completion import create_completion, get_completions, create_completion_stream
from tests.fixtures.data_source import (
    create_data_source,
    get_data_sources,
    test_connection,
    update_data_source,
    delete_data_source,
    get_schema,
    refresh_schema,
    get_metadata_resources,
    update_metadata_resources,
    bulk_update_tables,
    update_tables_status_delta,
    get_full_schema_paginated,
    list_demo_data_sources,
    install_demo_data_source,
)
from tests.fixtures.git_repository import (
    create_git_repository,
    get_git_repository,
    test_git_repository_connection,
    update_git_repository,
    delete_git_repository,
    index_git_repository,
    get_linked_instructions_count,
    sync_git_branch,
    push_build_to_git,
    get_git_repo_status,
    publish_build,
)
from tests.fixtures.instruction import create_instruction, create_global_instruction, get_instructions, get_instruction, update_instruction, delete_instruction, get_instructions_for_data_source, get_instruction_categories, get_instruction_statuses, create_label, list_labels, update_label, delete_label, get_instructions_by_source_type, unlink_instruction_from_git, bulk_update_instructions, bulk_delete_instructions
from tests.fixtures.entity import get_entities, get_entity, create_global_entity
from tests.fixtures.console_metrics import get_console_metrics, get_console_metrics_comparison, get_timeseries_metrics, get_table_usage_metrics, get_top_users_metrics, get_recent_negative_feedback, get_diagnosis_dashboard_metrics, get_agent_execution_summaries, create_test_data_for_console, get_tool_usage_metrics, get_llm_usage_metrics
from tests.fixtures.mention import get_available_mentions
from tests.fixtures.eval import create_test_suite, get_test_suites, create_test_case, get_test_cases, get_test_case, get_test_suite, create_test_run, get_test_runs, get_test_run, get_suites_summary
from tests.fixtures.file import upload_file, upload_csv_file, upload_excel_file, get_files, get_files_by_report, remove_file_from_report
from tests.fixtures.organization_settings import get_organization_settings, update_organization_settings, upload_organization_icon, delete_organization_icon, get_organization_icon
from tests.fixtures.api_key import create_api_key, list_api_keys, delete_api_key, api_key_request
from tests.fixtures.mcp import enable_mcp, disable_mcp
from tests.fixtures.build import (
    get_builds,
    get_build,
    get_main_build,
    get_build_contents,
    get_build_diff,
    get_build_diff_detailed,
    rollback_build,
)

from main import app


# ============================================================================
# Git Write Credentials
# ============================================================================

@pytest.fixture
def git_write_credentials():
    """Returns git write credentials or None if not configured.
    
    Set environment variables:
    - TEST_GIT_PAT: Personal Access Token for push/PR tests
    - TEST_GIT_SSH_KEY: SSH key for push tests (alternative to PAT)
    - TEST_GIT_WRITE_REPO_URL: Writable test repository URL
    """
    pat = os.environ.get("TEST_GIT_PAT")
    ssh_key = os.environ.get("TEST_GIT_SSH_KEY")
    repo_url = os.environ.get("TEST_GIT_WRITE_REPO_URL")
    
    if pat or ssh_key:
        return {
            "pat": pat,
            "ssh_key": ssh_key,
            "repo_url": repo_url,
        }
    return None


@pytest.fixture
def skip_without_git_write(git_write_credentials):
    """Skip test if git write credentials not configured."""
    if not git_write_credentials:
        pytest.skip(
            "Git write credentials not configured. "
            "Set TEST_GIT_PAT or TEST_GIT_SSH_KEY env vars to enable."
        )
    return git_write_credentials


@pytest.fixture(scope="session")
def db_backend(request):
    """Get the database backend from CLI option."""
    return request.config.getoption("--db", default="sqlite")


@pytest.fixture(scope="session")
def alembic_config(db_backend):
    """Create Alembic configuration object."""
    test_url = os.environ.get("TEST_DATABASE_URL", settings.TEST_DATABASE_URL)
    print(f"Using test database URL: {test_url} (backend: {db_backend})")
    
    # Ensure required directories exist for tests
    backend_dir = os.path.dirname(os.path.dirname(__file__))
    
    for dir_name in ["uploads/files", "uploads/branding"]:
        dir_path = os.path.join(backend_dir, dir_name)
        if not os.path.exists(dir_path):
            print(f"Creating {dir_name} directory: {dir_path}")
            os.makedirs(dir_path, exist_ok=True)
    
    if db_backend == "sqlite":
        # Ensure the database directory exists for SQLite
        db_dir = os.path.join(backend_dir, "db")
        if not os.path.exists(db_dir):
            print(f"Creating database directory: {db_dir}")
            os.makedirs(db_dir, exist_ok=True)
        
        # Convert async driver to sync for alembic
        sync_url = test_url.replace('sqlite+aiosqlite:', 'sqlite:')
    else:
        # PostgreSQL - ensure we use sync driver for alembic
        sync_url = test_url.replace('postgresql+asyncpg://', 'postgresql://')
    
    alembic_cfg = Config("alembic.ini")
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    return alembic_cfg


def _reset_postgres_schema(alembic_config):
    """Drop and recreate public schema to clean up ENUMs, types, etc."""
    from sqlalchemy import create_engine, text
    engine = create_engine(alembic_config.get_main_option("sqlalchemy.url"))
    with engine.connect() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
        conn.execute(text("GRANT ALL ON SCHEMA public TO public"))
        conn.commit()
    engine.dispose()


def _dispose_async_engine():
    """Dispose of the async engine to release all SQLite connections."""
    from app.dependencies import engine
    import asyncio
    import gc
    import time
    
    async def _dispose():
        await engine.dispose()
    
    # Force garbage collection first to clean up any lingering connections
    gc.collect()
    
    # Run dispose in a fresh event loop
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_dispose())
        loop.close()
    except Exception as e:
        print(f"Warning: Failed to dispose engine: {e}")
    
    # Force another GC and small delay to ensure SQLite releases file locks
    gc.collect()
    time.sleep(0.1)


@pytest.fixture(scope="function", autouse=True)
def run_migrations(alembic_config, db_backend):
    """Run migrations per test function for isolation."""
    
    if db_backend == "postgres":
        # PostgreSQL: reset schema BEFORE test to avoid stale async connections
        print("Resetting PostgreSQL schema...")
        _reset_postgres_schema(alembic_config)
    elif db_backend == "sqlite":
        # SQLite: dispose engine before migrations to release any stale connections
        _dispose_async_engine()
    
    print("Starting migrations...")
    command.upgrade(alembic_config, "head")
    print("Migrations completed!")
    
    yield
    
    # Cleanup after test
    if db_backend == "sqlite":
        # SQLite: dispose engine first to release connections, then downgrade and remove file
        print("Cleaning up SQLite...")
        _dispose_async_engine()
        command.downgrade(alembic_config, "base")
        db_file = alembic_config.get_main_option("sqlalchemy.url").replace('sqlite:///', '')
        if os.path.exists(db_file):
            os.remove(db_file)
    # PostgreSQL cleanup happens at START of next test (or container shutdown)