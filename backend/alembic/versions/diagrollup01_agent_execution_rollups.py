"""Diagnosis explorer: rollup columns on agent_executions, run attribution on
llm_usage_records, and the indexes the query language filters on.

The rollup columns denormalise what the diagnosis page filters and sorts on
so every read hits ``agent_executions`` alone (plus one EXISTS on
``tool_executions``): prompt/error text, platform, feedback, judge scores,
model/provider, cost and tokens, tool counts, turn index. They are written by
``app.services.diagnosis.rollup.refresh_rollup`` at run end and on feedback,
judge and late-usage events; a background sweep at app startup
(``app.services.diagnosis.sweep``) fills them for existing rows, newest first,
so this migration only adds columns and indexes and never touches data.

Revision ID: diagrollup01
Revises: ldapsecurity01
Create Date: 2026-09-08
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "diagrollup01"
down_revision: Union[str, None] = "ldapsecurity01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_AE_COLUMNS = [
    sa.Column("prompt_text", sa.Text(), nullable=True),
    sa.Column("error_text", sa.Text(), nullable=True),
    sa.Column("platform", sa.String(), nullable=True),
    sa.Column("feedback_direction", sa.Integer(), nullable=True),
    sa.Column("feedback_message", sa.Text(), nullable=True),
    sa.Column("judge_response_score", sa.Integer(), nullable=True),
    sa.Column("judge_instructions_score", sa.Integer(), nullable=True),
    sa.Column("judge_context_score", sa.Integer(), nullable=True),
    sa.Column("primary_model_id", sa.String(), nullable=True),
    sa.Column("primary_provider", sa.String(), nullable=True),
    sa.Column("total_cost_usd", sa.Float(), nullable=True),
    sa.Column("prompt_tokens", sa.Integer(), nullable=True),
    sa.Column("completion_tokens", sa.Integer(), nullable=True),
    sa.Column("total_tokens", sa.Integer(), nullable=True),
    sa.Column("tool_count", sa.Integer(), nullable=True),
    sa.Column("failed_tool_count", sa.Integer(), nullable=True),
    sa.Column("turn_index", sa.Integer(), nullable=True),
    sa.Column("cost_is_partial", sa.Boolean(), nullable=True),
    sa.Column("rollup_at", sa.DateTime(), nullable=True),
    sa.Column("rollup_version", sa.Integer(), nullable=True),
]

# (name, table, columns). Every one leads with organization_id so the
# planner narrows to the org first, then the predicate, then the time range.
_INDEXES = [
    ("ix_ae_org_created_id", "agent_executions", ["organization_id", "created_at", "id"]),
    ("ix_ae_org_status_created", "agent_executions", ["organization_id", "status", "created_at"]),
    ("ix_ae_org_user_created", "agent_executions", ["organization_id", "user_id", "created_at"]),
    ("ix_ae_org_model_created", "agent_executions", ["organization_id", "primary_model_id", "created_at"]),
    ("ix_ae_org_provider_created", "agent_executions", ["organization_id", "primary_provider", "created_at"]),
    ("ix_ae_org_feedback_created", "agent_executions", ["organization_id", "feedback_direction", "created_at"]),
    ("ix_ae_org_cost_created", "agent_executions", ["organization_id", "total_cost_usd", "created_at"]),
    ("ix_ae_org_duration_created", "agent_executions", ["organization_id", "total_duration_ms", "created_at"]),
    ("ix_ae_report_id", "agent_executions", ["report_id"]),
    # The startup sweep's "anything left to index?" check.
    ("ix_ae_rollup_version", "agent_executions", ["rollup_version"]),
    ("ix_tool_exec_ae_tool", "tool_executions", ["agent_execution_id", "tool_name"]),
    ("ix_tool_exec_tool_status", "tool_executions", ["tool_name", "status"]),
    ("ix_llm_usage_agent_execution", "llm_usage_records", ["agent_execution_id"]),
    ("ix_llm_usage_report_created", "llm_usage_records", ["report_id", "created_at"]),
]


def _existing_columns(inspector, table_name):
    try:
        return {c["name"] for c in inspector.get_columns(table_name)}
    except Exception:
        return set()


def _existing_indexes(inspector, table_name):
    try:
        return {ix["name"] for ix in inspector.get_indexes(table_name)}
    except Exception:
        return set()


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # BOW training tables use the same diagnosis data, with persisted access lineage.
    for table, column in (("reports", "bow_source_access"), ("entities", "bow_source_access"), ("queries", "source_refs")):
        if column not in _existing_columns(inspector, table):
            op.add_column(table, sa.Column(column, sa.JSON(), nullable=True))

    ae_cols = _existing_columns(inspector, "agent_executions")
    with op.batch_alter_table("agent_executions") as batch_op:
        for col in _AE_COLUMNS:
            if col.name not in ae_cols:
                batch_op.add_column(col.copy())

    usage_cols = _existing_columns(inspector, "llm_usage_records")
    if "agent_execution_id" not in usage_cols:
        with op.batch_alter_table("llm_usage_records") as batch_op:
            batch_op.add_column(sa.Column("agent_execution_id", sa.String(36), nullable=True))

    # Re-inspect after the DDL so index existence is checked against the new shape.
    inspector = sa.inspect(bind)
    for name, table, cols in _INDEXES:
        if name not in _existing_indexes(inspector, table):
            op.create_index(name, table, cols)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for name, table, cols in reversed(_INDEXES):
        if name in _existing_indexes(inspector, table):
            op.drop_index(name, table)

    for table, column in (("queries", "source_refs"), ("entities", "bow_source_access"), ("reports", "bow_source_access")):
        if column in _existing_columns(inspector, table):
            op.drop_column(table, column)

    usage_cols = _existing_columns(inspector, "llm_usage_records")
    if "agent_execution_id" in usage_cols:
        with op.batch_alter_table("llm_usage_records") as batch_op:
            batch_op.drop_column("agent_execution_id")

    ae_cols = _existing_columns(inspector, "agent_executions")
    with op.batch_alter_table("agent_executions") as batch_op:
        for col in reversed(_AE_COLUMNS):
            if col.name in ae_cols:
                batch_op.drop_column(col.name)
