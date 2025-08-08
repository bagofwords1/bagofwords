from logging.config import fileConfig

import sqlalchemy
from sqlalchemy import engine_from_config
from sqlalchemy import pool
from sqlalchemy import create_engine
from sqlalchemy.engine.url import make_url

from alembic import context
from alembic.operations import ops

from app.settings.config import settings

from app.models.base import BaseSchema
from app.models.report import Report
from app.models.widget import Widget
from app.models.step import Step
from app.models.completion import Completion
from app.models.file import File
from app.models.report_file_association import report_file_association
from app.models.user import User
from app.models.organization import Organization
from app.models.membership import Membership
from app.models.data_source import DataSource
from app.models.report_data_source_association import report_data_source_association
from app.models.sheet_schema import SheetSchema
from app.models.prompt import Prompt
from app.models.plan import Plan
from app.models.memory import Memory
from app.models.mention import Mention
from app.models.file_tag import FileTag
from app.models.text_widget import TextWidget
from app.models.llm_provider import LLMProvider
from app.models.llm_model import LLMModel
from app.models.oauth_account import OAuthAccount
from app.models.datasource_table import DataSourceTable
from app.models.git_repository import GitRepository
from app.models.metadata_indexing_job import MetadataIndexingJob
from app.models.metadata_resource import MetadataResource
from app.models.organization_settings import OrganizationSettings
from app.models.external_platform import ExternalPlatform
from app.models.external_user_mapping import ExternalUserMapping
from app.models.instruction import Instruction
from app.models.instruction import instruction_data_source_association
from app.models.completion_feedback import CompletionFeedback
from app.models.data_source_membership import DataSourceMembership

from app.settings.config import settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config



def get_db_url():
    if settings.TESTING:
        return settings.TEST_DATABASE_URL
    else:
        url = make_url(settings.bow_config.database.url)
        if url.drivername.startswith('postgres'):
            return url.set(drivername="postgresql")
        elif url.drivername.startswith('sqlite'):
            return str(url)
        return str(url)



# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# add your model's MetaData object here
# for 'autogenerate' support
# from myapp import mymodel
# target_metadata = mymodel.Base.metadata
from app.models import base
target_metadata = base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # Enable batch operations for SQLite
    )

    with context.begin_transaction():
        context.run_migrations()

def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    url = get_db_url()
    connectable = create_engine(url, poolclass=pool.NullPool)
    
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # Enable batch operations for SQLite
        )

        with context.begin_transaction():
            context.run_migrations()




if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
