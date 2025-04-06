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

from tests.fixtures.client import test_client
from tests.fixtures.user import create_user
from tests.fixtures.auth import login_user
from tests.fixtures.organization import create_organization, add_organization_member, get_organization_members, update_organization_member, remove_organization_member, get_user_organizations
from tests.fixtures.llm import create_llm_provider_and_models, get_models, get_default_model, set_llm_provider_as_default, toggle_llm_active_status, delete_llm_provider
from tests.fixtures.report import create_report, get_reports, get_report, update_report, delete_report, publish_report, rerun_report, schedule_report, get_public_report
from tests.fixtures.completion import create_completion, get_completions
from tests.fixtures.data_source import create_data_source, get_data_sources, test_connection

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
    """Run migrations before each test and downgrade after."""
    print("Starting migrations...")
    command.upgrade(alembic_config, "head")
    print("Migrations completed!")
    yield
    print("Downgrading migrations...")
    command.downgrade(alembic_config, "base")
    # Clean up test database file
    db_file = alembic_config.get_main_option("sqlalchemy.url").replace('sqlite:///', '')
    if os.path.exists(db_file):
        os.remove(db_file)

def pytest_configure(config):
    config.addinivalue_line(
        "markers", "e2e: marks tests as end-to-end tests"
    )