"""agent sandboxes: backends, environments, sandboxes, runs, events

Revision ID: agentsbx01
Revises: mrgheads12
Create Date: 2026-11-26

Tables for agent sandboxes (BOW-229): containers on operator-provided servers
where a report's agent runs code. The feature ships behind the deployment
flag BOW_AGENT_SANDBOXES_ENABLED and is off by default.

This revision only ADDS tables. It alters no existing table:

  sandbox_backends        a place sandboxes run
  sandbox_environments    admin-defined templates
  sandbox_agent_defaults  an agent's default environment (a table, so
                          data_sources is not altered)
  sandboxes               one per report
  sandbox_runs            every run
  sandbox_events          the sandbox timeline
  sandbox_egress_log      domains reached and refused
  sandbox_exposures       reserved for hosted apps; created empty

References to reports, users, completions and data sources are plain indexed
ids, not foreign keys, so nothing here can block or cascade into them.

One non-deleted sandbox per report is enforced by a partial unique index.

Idempotent in both directions (the dashboard test corpus replays schema-only
revisions unstamped, see tests/fixtures/dashboard_corpus/alembic_at.py).
"""
import sqlalchemy as sa
from alembic import op

revision = "agentsbx01"
down_revision = "mrgheads12"
branch_labels = None
depends_on = None

_LIVE = sa.text("deleted_at IS NULL")

# Dropped in this order (children first).
TABLES = [
    "sandbox_exposures",
    "sandbox_egress_log",
    "sandbox_events",
    "sandbox_runs",
    "sandboxes",
    "sandbox_agent_defaults",
    "sandbox_environments",
    "sandbox_backends",
]


def _base_columns():
    return [
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    ]


def _org():
    return sa.Column("organization_id", sa.String(length=36), sa.ForeignKey("organizations.id"), nullable=False)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _index(name: str, table: str, columns, **kw) -> None:
    existing = {ix["name"] for ix in sa.inspect(op.get_bind()).get_indexes(table)}
    if name not in existing:
        op.create_index(name, table, columns, **kw)


def _partial_unique(name: str, table: str, columns) -> None:
    _index(name, table, columns, unique=True, sqlite_where=_LIVE, postgresql_where=_LIVE)


def upgrade() -> None:
    if not _has_table("sandbox_backends"):
        op.create_table(
            "sandbox_backends",
            *_base_columns(),
            _org(),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("type", sa.String(length=40), nullable=False),
            sa.Column("config", sa.JSON(), nullable=False),
            sa.Column("credentials", sa.Text(), nullable=True),
            sa.Column("driver_state", sa.JSON(), nullable=False),
            sa.Column("capabilities", sa.JSON(), nullable=False),
            sa.Column("labels", sa.JSON(), nullable=False),
            sa.Column("capacity", sa.Integer(), nullable=False, server_default="20"),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("bootstrap_status", sa.String(length=20), nullable=False, server_default="none"),
            sa.Column("bootstrap_steps", sa.JSON(), nullable=False),
            sa.Column("bootstrapped_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="unknown"),
            sa.Column("last_checked_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_by", sa.String(length=36), nullable=True),
        )
    _index("ix_sandbox_backends_id", "sandbox_backends", ["id"], unique=True)
    _index("ix_sandbox_backends_organization_id", "sandbox_backends", ["organization_id"])
    _partial_unique("uq_sandbox_backends_live_name", "sandbox_backends", ["organization_id", "name"])

    if not _has_table("sandbox_environments"):
        op.create_table(
            "sandbox_environments",
            *_base_columns(),
            _org(),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("image", sa.String(length=500), nullable=False),
            sa.Column("image_digest", sa.String(length=200), nullable=True),
            sa.Column("registry_username", sa.String(length=200), nullable=True),
            sa.Column("registry_password", sa.Text(), nullable=True),
            sa.Column("setup_script", sa.Text(), nullable=True),
            sa.Column("cpus", sa.Float(), nullable=False, server_default="1"),
            sa.Column("memory_mb", sa.Integer(), nullable=False, server_default="2048"),
            sa.Column("disk_mb", sa.Integer(), nullable=False, server_default="5120"),
            sa.Column("run_timeout_seconds", sa.Integer(), nullable=False, server_default="600"),
            sa.Column("network_policy", sa.String(length=20), nullable=False, server_default="none"),
            sa.Column("allowed_domains", sa.JSON(), nullable=False),
            sa.Column("env_vars", sa.JSON(), nullable=False),
            sa.Column("secrets", sa.Text(), nullable=True),
            sa.Column("backend_labels", sa.JSON(), nullable=False),
            sa.Column("require_strong_isolation", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("ports", sa.JSON(), nullable=False),
            sa.Column("warm_pool_size", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_by", sa.String(length=36), nullable=True),
        )
    _index("ix_sandbox_environments_id", "sandbox_environments", ["id"], unique=True)
    _index("ix_sandbox_environments_organization_id", "sandbox_environments", ["organization_id"])
    _partial_unique("uq_sandbox_environments_live_name", "sandbox_environments", ["organization_id", "name"])

    if not _has_table("sandbox_agent_defaults"):
        op.create_table(
            "sandbox_agent_defaults",
            *_base_columns(),
            _org(),
            sa.Column("data_source_id", sa.String(length=36), nullable=False),
            sa.Column("environment_id", sa.String(length=36), sa.ForeignKey("sandbox_environments.id"), nullable=False),
        )
    _index("ix_sandbox_agent_defaults_id", "sandbox_agent_defaults", ["id"], unique=True)
    _index("ix_sandbox_agent_defaults_organization_id", "sandbox_agent_defaults", ["organization_id"])
    _index("ix_sandbox_agent_defaults_environment_id", "sandbox_agent_defaults", ["environment_id"])
    _partial_unique("uq_sandbox_agent_defaults_live_data_source", "sandbox_agent_defaults", ["data_source_id"])

    if not _has_table("sandboxes"):
        op.create_table(
            "sandboxes",
            *_base_columns(),
            _org(),
            sa.Column("report_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("backend_id", sa.String(length=36), sa.ForeignKey("sandbox_backends.id"), nullable=True),
            sa.Column("environment_id", sa.String(length=36), sa.ForeignKey("sandbox_environments.id"), nullable=True),
            sa.Column("image_digest", sa.String(length=200), nullable=True),
            sa.Column("driver_ref", sa.JSON(), nullable=True),
            sa.Column("state", sa.String(length=20), nullable=False, server_default="provisioning"),
            sa.Column("token", sa.Text(), nullable=True),
            sa.Column("limits", sa.JSON(), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(), nullable=True),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("stopped_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
        )
    _index("ix_sandboxes_id", "sandboxes", ["id"], unique=True)
    for col in ("organization_id", "report_id", "user_id", "backend_id", "environment_id", "state"):
        _index(f"ix_sandboxes_{col}", "sandboxes", [col])
    _index("ix_sandboxes_backend_state", "sandboxes", ["backend_id", "state"])
    # One non-deleted sandbox per report, enforced by the database.
    _partial_unique("uq_sandboxes_one_live_per_report", "sandboxes", ["report_id"])

    if not _has_table("sandbox_runs"):
        op.create_table(
            "sandbox_runs",
            *_base_columns(),
            sa.Column("sandbox_id", sa.String(length=36), sa.ForeignKey("sandboxes.id"), nullable=False),
            _org(),
            sa.Column("report_id", sa.String(length=36), nullable=False),
            sa.Column("user_id", sa.String(length=36), nullable=True),
            sa.Column("completion_id", sa.String(length=36), nullable=True),
            sa.Column("tool_execution_id", sa.String(length=36), nullable=True),
            sa.Column("language", sa.String(length=20), nullable=True),
            sa.Column("command", sa.JSON(), nullable=True),
            sa.Column("code", sa.Text(), nullable=True),
            sa.Column("code_sha256", sa.String(length=64), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="running"),
            sa.Column("exit_code", sa.Integer(), nullable=True),
            sa.Column("stdout", sa.Text(), nullable=True),
            sa.Column("stderr", sa.Text(), nullable=True),
            sa.Column("input_file_ids", sa.JSON(), nullable=False),
            sa.Column("artifact_file_ids", sa.JSON(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=True),
            sa.Column("finished_at", sa.DateTime(), nullable=True),
        )
    _index("ix_sandbox_runs_id", "sandbox_runs", ["id"], unique=True)
    for col in ("organization_id", "report_id", "user_id"):
        _index(f"ix_sandbox_runs_{col}", "sandbox_runs", [col])
    _index("ix_sandbox_runs_sandbox_started", "sandbox_runs", ["sandbox_id", "started_at"])

    if not _has_table("sandbox_events"):
        op.create_table(
            "sandbox_events",
            *_base_columns(),
            sa.Column("sandbox_id", sa.String(length=36), sa.ForeignKey("sandboxes.id"), nullable=False),
            sa.Column("type", sa.String(length=60), nullable=False),
            sa.Column("actor_user_id", sa.String(length=36), nullable=True),
            sa.Column("details", sa.JSON(), nullable=False),
        )
    _index("ix_sandbox_events_id", "sandbox_events", ["id"], unique=True)
    _index("ix_sandbox_events_sandbox_created", "sandbox_events", ["sandbox_id", "created_at"])

    if not _has_table("sandbox_egress_log"):
        op.create_table(
            "sandbox_egress_log",
            *_base_columns(),
            sa.Column("sandbox_id", sa.String(length=36), sa.ForeignKey("sandboxes.id"), nullable=False),
            sa.Column("domain", sa.String(length=255), nullable=False),
            sa.Column("decision", sa.String(length=10), nullable=False),
            sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("first_at", sa.DateTime(), nullable=True),
            sa.Column("last_at", sa.DateTime(), nullable=True),
        )
    _index("ix_sandbox_egress_log_id", "sandbox_egress_log", ["id"], unique=True)
    _index("uq_sandbox_egress_log_domain", "sandbox_egress_log", ["sandbox_id", "domain", "decision"], unique=True)

    # Reserved for hosted apps. Created empty; nothing reads or writes it in v1.
    if not _has_table("sandbox_exposures"):
        op.create_table(
            "sandbox_exposures",
            *_base_columns(),
            sa.Column("sandbox_id", sa.String(length=36), sa.ForeignKey("sandboxes.id"), nullable=False),
            sa.Column("port", sa.Integer(), nullable=False),
            sa.Column("level", sa.String(length=20), nullable=False),
            sa.Column("slug", sa.String(length=120), nullable=True, unique=True),
            sa.Column("created_by", sa.String(length=36), nullable=True),
        )
    _index("ix_sandbox_exposures_id", "sandbox_exposures", ["id"], unique=True)
    _index("ix_sandbox_exposures_sandbox_id", "sandbox_exposures", ["sandbox_id"])


def downgrade() -> None:
    for table in TABLES:
        if _has_table(table):
            op.drop_table(table)
