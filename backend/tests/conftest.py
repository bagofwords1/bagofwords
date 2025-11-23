import asyncio
import pytest
import os
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Generator, AsyncGenerator
from fastapi.testclient import TestClient
from alembic.config import Config
from alembic import command

from app.models.base import Base
from app.settings.config import settings
from app.settings.database import create_async_database_engine, create_async_session_factory

# Ensure the application uses the test database/engine during tests
settings.TESTING = True

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
)
from tests.fixtures.git_repository import (
    create_git_repository,
    get_git_repository,
    test_git_repository_connection,
    update_git_repository,
    delete_git_repository,
    index_git_repository,
)
from tests.fixtures.instruction import create_instruction, create_global_instruction, get_instructions, get_instruction, update_instruction, delete_instruction, get_instructions_for_data_source, get_instruction_categories, get_instruction_statuses, create_label, list_labels, update_label, delete_label
from tests.fixtures.entity import get_entities, get_entity, create_global_entity
from tests.fixtures.console_metrics import get_console_metrics, get_console_metrics_comparison, get_timeseries_metrics, get_table_usage_metrics, get_top_users_metrics, get_recent_negative_feedback, get_diagnosis_dashboard_metrics, get_agent_execution_summaries, create_test_data_for_console, get_tool_usage_metrics, get_llm_usage_metrics
from tests.fixtures.mention import get_available_mentions
from tests.fixtures.eval import create_test_suite, get_test_suites, create_test_case, get_test_cases, get_test_case, get_test_suite

from main import app

@pytest.fixture(scope="session")
def alembic_config():
    """Create Alembic configuration object."""
    print(f"Using test database URL: {settings.TEST_DATABASE_URL}")
    
    # Ensure the database directory exists
    db_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "db")
    if not os.path.exists(db_dir):
        print(f"Creating database directory: {db_dir}")
        os.makedirs(db_dir, exist_ok=True)
    
    alembic_cfg = Config("alembic.ini")
    # Convert aiosqlite to sqlite for alembic
    sync_url = settings.TEST_DATABASE_URL.replace('sqlite+aiosqlite:', 'sqlite:')
    # Add unique test database for each test file
    sync_url = sync_url.replace('test.db', f'test_{os.getpid()}.db')
    alembic_cfg.set_main_option("sqlalchemy.url", sync_url)
    return alembic_cfg

@pytest.fixture(scope="function", autouse=True)
def run_migrations(alembic_config):
    """Run migrations once per test session to avoid SQLite locking issues."""
    print("Starting migrations...")
    command.upgrade(alembic_config, "head")
    print("Migrations completed!")
    yield
    print("Downgrading migrations...")
    command.downgrade(alembic_config, "base")
    # Clean up test database file at the end of the session
    db_file = alembic_config.get_main_option("sqlalchemy.url").replace('sqlite:///', '')
    if os.path.exists(db_file):
        os.remove(db_file)

def pytest_configure(config):
    config.addinivalue_line(
        "markers", "e2e: marks tests as end-to-end tests"
    )