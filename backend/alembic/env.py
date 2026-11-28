from logging.config import fileConfig

import sqlalchemy
from sqlalchemy import engine_from_config, event
from sqlalchemy import pool
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

from alembic import context
from alembic.operations import ops

from app.settings.config import settings
from app.settings.db_auth import get_auth_provider

from app.models.base import BaseSchema
from app.models.report import Report
from app.models.artifact_release import ArtifactRelease, ArtifactShare
from app.models.artifact_resource import ArtifactResource, ArtifactRecord, ArtifactRecordIndex, ArtifactMutation, ArtifactView, ArtifactFileBinding, ArtifactRateBucket
from app.models.artifact import Artifact, ArtifactVersion
from app.models.widget import Widget
from app.models.step import Step
from app.models.completion import Completion
from app.models.file import File
from app.models.report_file_association import report_file_association
from app.models.saml import SAMLIdentity, SAMLRequest
from app.models.user import User
from app.models.organization import Organization
from app.models.membership import Membership
from app.models.data_source import DataSource
from app.models.report_data_source_association import report_data_source_association
from app.models.data_source_file_association import data_source_file_association
from app.models.sheet_schema import SheetSchema
from app.models.prompt import Prompt
from app.models.prompt_run import PromptRun
from app.models.plan import Plan
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
from app.models.instance_settings import InstanceSettings
from app.models.external_platform import ExternalPlatform
from app.models.external_user_mapping import ExternalUserMapping
from app.models.external_platform_agent import ExternalPlatformAgent
from app.models.external_platform_channel import ExternalPlatformChannel
from app.models.instruction import Instruction
from app.models.instruction import instruction_data_source_association
from app.models.completion_feedback import CompletionFeedback
from app.models.data_source_membership import DataSourceMembership
from app.models.instruction_reference import InstructionReference
from app.models.table_stats import TableStats
from app.models.column_stats import ColumnStats
from app.models.table_usage_event import TableUsageEvent
from app.models.table_feedback_event import TableFeedbackEvent
from app.models.agent_execution import AgentExecution
from app.models.email_delivery_claim import EmailDeliveryClaim
from app.models.plan_decision import PlanDecision
from app.models.tool_execution import ToolExecution
from app.models.context_snapshot import ContextSnapshot
from app.models.report_context_state import ReportContextState
from app.models.completion_block import CompletionBlock
from app.models.dashboard_layout_version import DashboardLayoutVersion
from app.models.query import Query
from app.models.visualization import Visualization
from app.models.user_data_source_credentials import UserDataSourceCredentials
from app.models.user_data_source_overlay import UserDataSourceTable as UserOverlayTable, UserDataSourceColumn as UserOverlayColumn
from app.models.connection import Connection
from app.models.connection_indexing import ConnectionIndexing
from app.models.connection_rate_limit_counter import ConnectionRateLimitCounter
from app.models.query_execution import QueryExecution
from app.models.connection_table import ConnectionTable
from app.models.file_index import FileIndexFile, FileIndexSection
from app.models.sub_agent import SubAgentRun, SubAgentWave
from app.models.sub_agent_message import SubAgentMessage
from app.models.note import Note
from app.models.memory_entry import MemoryEntry
from app.models.agent_list import AgentList, AgentListRow, AgentListRowRevision
from app.models.connection_tool import ConnectionTool
from app.models.user_connection_tool import UserConnectionTool
from app.models.user_connection_tool_preference import UserConnectionToolPreference
from app.models.user_connector_preference import UserConnectorPreference
from app.models.tool_confirmation import ToolConfirmation
from app.models.officejs_pending_result import OfficeJsPendingResult
from app.models.login_exchange_code import LoginExchangeCode
from app.models.data_source_connection_tool import DataSourceConnectionTool
from app.models.data_source_connection_setting import DataSourceConnectionSetting
from app.models.domain_connection import domain_connection
from app.models.user_connection_credentials import UserConnectionCredentials
from app.models.user_connection_overlay import UserConnectionTable, UserConnectionColumn
from app.models.entity import Entity
from app.models.eval import TestSuite
from app.models.eval import TestCase
from app.models.eval import TestRun
from app.models.eval import TestResult
from app.models.instruction_label import InstructionLabel
from app.models.instruction_label import instruction_label_association
from app.models.instruction_directory import InstructionDirectory, InstructionDirectoryPlacement
from app.models.llm_usage_record import LLMUsageRecord
from app.models.api_key import ApiKey
from app.models.service_account import ServiceAccount
from app.models.scheduled_prompt import ScheduledPrompt
from app.models.instruction_build import InstructionBuild
from app.models.oauth_server import OAuthClient, OAuthAuthorizationCode, OAuthAccessToken
from app.models.host_application import HostApplication, ArtifactHostApplication, HostAssertionReplay
from app.models.artifact_activity import ArtifactActivityEvent
from app.ee.scim.models import ScimToken
from app.ee.audit.streams.models import AuditLogStream
from app.models.role import Role
from app.models.group import Group
from app.models.group_membership import GroupMembership
from app.models.role_assignment import RoleAssignment
from app.models.resource_grant import ResourceGrant
from app.models.sandbox import (
    SandboxBackend, SandboxEnvironment, SandboxAgentDefault, Sandbox,
    SandboxRun, SandboxEvent, SandboxEgressLog, SandboxExposure,
)
from app.models.report_share import ReportShare
from app.models.report_star import ReportStar
from app.models.report_view import ReportView
from app.models.step_user_result import StepUserResult
from app.models.entity_user_result import EntityUserResult
from app.models.project import Project, project_data_source_association, project_file_association
from app.models.agent_automation_run import AgentAutomationRun
from app.models.review_item import ReviewItem
from app.models.notification import Notification
from app.models.dashboard_access_request import DashboardAccessRequest
from app.models.agent_checkin import AgentCheckin
from app.models.dream_run import DreamRun
from app.models.usage_policy import (
    UsagePolicy,
    UsagePolicyAssignment,
    UsagePolicyConnectionOverride,
    UsageCounter,
    UsageEvent,
)

from app.settings.config import settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config



def get_db_url():
    if settings.TESTING:
        return settings.TEST_DATABASE_URL
    else:
        db = settings.bow_config.database
        raw_url = db.get_url()
        url = make_url(raw_url)
        if url.drivername.startswith('postgres'):
            return url.set(drivername="postgresql")
        elif url.drivername.startswith('sqlite'):
            return str(url)
        return str(url)



# Interpret the config file for Python logging.
# This line sets up loggers basically. Loggers that already exist stay enabled:
# the test suite runs migrations in-process, and the default (disabling them)
# silenced whatever app loggers had been imported first, so log-asserting tests
# passed or failed by which test came first in the worker.
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

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

def _attach_migration_iam_hook(engine):
    """Attach IAM auth hook to the migration engine if configured."""
    if settings.TESTING:
        return
    db_config = settings.bow_config.database
    if not db_config.uses_iam_auth:
        return
    provider = get_auth_provider(db_config)
    host = db_config.host
    port = db_config.port
    username = db_config.username

    @event.listens_for(engine, "do_connect")
    def inject_token(dialect, conn_rec, cargs, cparams):
        cparams["password"] = provider.get_password(host, port, username)


# Every replica runs `alembic upgrade head` when it starts (start.sh), so two
# pods of a rolling update or a scaled deployment can race the same
# migration. On Postgres the first one takes this advisory lock and migrates;
# the others wait, then read the version table, find the database at head and
# do nothing.
#
# The lock is session-level, held from before the first revision until the
# run ends. A transaction-level lock is not enough: a revision that builds an
# index CONCURRENTLY runs in an autocommit block, which commits the migration
# transaction and would release a transaction lock mid-chain, letting a
# waiting pod migrate the remaining revisions alongside this one. Migrations
# connect with their own NullPool connection, one session for the whole run;
# run them on a direct connection, not through a transaction-mode pooler,
# which could put the unlock on another backend. Closing the session also
# releases the lock, so a pod that dies mid-run never leaves it held.
#
# A waiting pod polls with pg_try_advisory_lock and holds no transaction
# between tries: CREATE INDEX CONCURRENTLY waits for every open transaction to
# end, so a pod blocked inside pg_advisory_lock would deadlock with the pod
# building the index. The wait is bounded so a pod stuck behind a dead session
# fails into start.sh's retry instead of hanging. SQLite is a single local
# file with a single writer.
MIGRATION_LOCK_KEY = 7262874361  # arbitrary, fixed for every BOW install
MIGRATION_LOCK_WAIT_SECONDS = 300
MIGRATION_LOCK_POLL_SECONDS = 1.0


def _take_migration_lock(connection) -> bool:
    """Take the session-level migration lock; True if one was taken."""
    if connection.dialect.name != "postgresql":
        return False
    import time

    deadline = time.monotonic() + MIGRATION_LOCK_WAIT_SECONDS
    while True:
        got = connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": MIGRATION_LOCK_KEY}).scalar()
        # End the implicit transaction every try opens: a waiting pod must hold
        # no transaction (see above), and alembic starts its own once locked.
        connection.commit()
        if got:
            return True
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"another migration held the lock for {MIGRATION_LOCK_WAIT_SECONDS}s; giving up so start.sh retries"
            )
        time.sleep(MIGRATION_LOCK_POLL_SECONDS)


def _release_migration_lock(connection) -> None:
    # Best effort: if the session is already broken (a failed revision), the
    # unlock would only hide that error, and closing the session releases the
    # lock anyway.
    try:
        if connection.in_transaction():
            connection.rollback()
        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_LOCK_KEY})
        connection.commit()
    except Exception:  # noqa: BLE001
        pass


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    url = get_db_url()
    db_config = settings.bow_config.database

    connect_args = {}
    if not settings.TESTING and db_config.uses_iam_auth and db_config.auth.ssl_mode:
        import os
        connect_args["sslmode"] = db_config.auth.ssl_mode
        if db_config.auth.ssl_mode == "verify-full":
            rds_ca = "/app/certs/rds-combined-ca-bundle.pem"
            if os.path.exists(rds_ca):
                connect_args["sslrootcert"] = rds_ca

    connectable = create_engine(url, poolclass=pool.NullPool, connect_args=connect_args)
    _attach_migration_iam_hook(connectable)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # Enable batch operations for SQLite
        )

        locked = _take_migration_lock(connection)
        try:
            with context.begin_transaction():
                context.run_migrations()
        finally:
            if locked:
                _release_migration_lock(connection)




if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
