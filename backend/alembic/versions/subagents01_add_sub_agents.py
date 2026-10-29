"""add sub_agent_waves / sub_agents tables and parent links on agent_executions

Revision ID: subagents01
Revises: mrgheads07
Create Date: 2026-10-12 00:00:00.000000

Sub-agents are delegated agent runs (mode=subagent) launched by the parent's
``delegate`` tool and fanned back in once per wave. The child is its own
``AgentExecution`` carrying the parent execution / tool execution ids, so the
existing per-run token and cost columns and the diagnosis rollup cover
children with no new accounting. See docs/design/sub-agents.md.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "subagents01"
down_revision: Union[str, None] = "mrgheads07"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Batch mode: SQLite cannot ALTER constraints in place (copy-and-move);
    # on Postgres this is a plain ALTER TABLE.
    with op.batch_alter_table("agent_executions") as batch_op:
        batch_op.add_column(sa.Column("parent_agent_execution_id", sa.String(length=36), nullable=True))
        batch_op.add_column(sa.Column("parent_tool_execution_id", sa.String(length=36), nullable=True))
        batch_op.create_foreign_key("fk_agent_executions_parent_agent_execution", "agent_executions", ["parent_agent_execution_id"], ["id"])
        batch_op.create_index("ix_agent_executions_parent_agent_execution_id", ["parent_agent_execution_id"])

    op.create_table(
        "sub_agent_waves",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("organization_id", sa.String(length=36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("report_id", sa.String(length=36), sa.ForeignKey("reports.id"), nullable=False),
        sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("parent_completion_id", sa.String(length=36), sa.ForeignKey("completions.id"), nullable=True),
        sa.Column("parent_execution_id", sa.String(length=36), sa.ForeignKey("agent_executions.id"), nullable=True),
        sa.Column("fan_in_completion_id", sa.String(length=36), sa.ForeignKey("completions.id"), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
        sa.Column("remaining", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deadline_at", sa.DateTime(), nullable=True),
        sa.Column("deadline_job_id", sa.String(length=96), nullable=True),
        sa.Column("fired_at", sa.DateTime(), nullable=True),
        sa.Column("parent_mode", sa.String(length=16), nullable=True),
        sa.Column("parent_prompt", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_sub_agent_waves_id", "sub_agent_waves", ["id"], unique=True)
    op.create_index("ix_sub_agent_waves_organization_id", "sub_agent_waves", ["organization_id"])
    op.create_index("ix_sub_agent_waves_report_id", "sub_agent_waves", ["report_id"])
    op.create_index("ix_sub_agent_waves_parent_completion_id", "sub_agent_waves", ["parent_completion_id"])
    op.create_index("ix_sub_agent_waves_parent_execution_id", "sub_agent_waves", ["parent_execution_id"])
    op.create_index("ix_sub_agent_waves_status", "sub_agent_waves", ["status"])
    op.create_index("ix_sub_agent_waves_report_status", "sub_agent_waves", ["report_id", "status"])

    op.create_table(
        "sub_agent_runs",
        sa.Column("id", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column("wave_id", sa.String(length=36), sa.ForeignKey("sub_agent_waves.id"), nullable=False),
        sa.Column("organization_id", sa.String(length=36), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("report_id", sa.String(length=36), sa.ForeignKey("reports.id"), nullable=False),
        sa.Column("wave_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("parent_tool_execution_id", sa.String(length=36), sa.ForeignKey("tool_executions.id"), nullable=True),
        sa.Column("parent_completion_id", sa.String(length=36), sa.ForeignKey("completions.id"), nullable=True),
        sa.Column("parent_execution_id", sa.String(length=36), sa.ForeignKey("agent_executions.id"), nullable=True),
        sa.Column("child_head_completion_id", sa.String(length=36), sa.ForeignKey("completions.id"), nullable=True),
        sa.Column("child_completion_id", sa.String(length=36), sa.ForeignKey("completions.id"), nullable=True),
        sa.Column("child_execution_id", sa.String(length=36), sa.ForeignKey("agent_executions.id"), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
        sa.Column("profile", sa.String(length=32), nullable=False, server_default="investigate"),
        sa.Column("goal", sa.Text(), nullable=False, server_default=""),
        sa.Column("context", sa.Text(), nullable=True),
        sa.Column("agent_ids_json", sa.JSON(), nullable=True),
        sa.Column("model_id", sa.String(length=36), sa.ForeignKey("llm_models.id"), nullable=True),
        sa.Column("model_name", sa.String(length=120), nullable=True),
        sa.Column("max_steps", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False, server_default="running"),
        sa.Column("exit_reason", sa.String(length=24), nullable=True),
        sa.Column("result_json", sa.JSON(), nullable=True),
        sa.Column("envelope_json", sa.JSON(), nullable=True),
        sa.Column("report_note_id", sa.String(length=36), sa.ForeignKey("notes.id"), nullable=True),
        sa.Column("steps_used", sa.Integer(), nullable=True),
        sa.Column("tokens_json", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_sub_agent_runs_id", "sub_agent_runs", ["id"], unique=True)
    op.create_index("ix_sub_agent_runs_wave_id", "sub_agent_runs", ["wave_id"])
    op.create_index("ix_sub_agent_runs_organization_id", "sub_agent_runs", ["organization_id"])
    op.create_index("ix_sub_agent_runs_report_id", "sub_agent_runs", ["report_id"])
    op.create_index("ix_sub_agent_runs_parent_tool_execution_id", "sub_agent_runs", ["parent_tool_execution_id"])
    op.create_index("ix_sub_agent_runs_child_completion_id", "sub_agent_runs", ["child_completion_id"])
    op.create_index("ix_sub_agent_runs_child_execution_id", "sub_agent_runs", ["child_execution_id"])
    op.create_index("ix_sub_agent_runs_status", "sub_agent_runs", ["status"])
    op.create_index("ix_sub_agent_runs_report_status", "sub_agent_runs", ["report_id", "status"])


def downgrade() -> None:
    for name in (
        "ix_sub_agent_runs_report_status", "ix_sub_agent_runs_status", "ix_sub_agent_runs_child_execution_id",
        "ix_sub_agent_runs_child_completion_id", "ix_sub_agent_runs_parent_tool_execution_id", "ix_sub_agent_runs_report_id",
        "ix_sub_agent_runs_organization_id", "ix_sub_agent_runs_wave_id", "ix_sub_agent_runs_id",
    ):
        op.drop_index(name, table_name="sub_agent_runs")
    op.drop_table("sub_agent_runs")
    for name in (
        "ix_sub_agent_waves_report_status", "ix_sub_agent_waves_status", "ix_sub_agent_waves_parent_execution_id",
        "ix_sub_agent_waves_parent_completion_id", "ix_sub_agent_waves_report_id", "ix_sub_agent_waves_organization_id",
        "ix_sub_agent_waves_id",
    ):
        op.drop_index(name, table_name="sub_agent_waves")
    op.drop_table("sub_agent_waves")
    with op.batch_alter_table("agent_executions") as batch_op:
        batch_op.drop_index("ix_agent_executions_parent_agent_execution_id")
        batch_op.drop_constraint("fk_agent_executions_parent_agent_execution", type_="foreignkey")
        batch_op.drop_column("parent_tool_execution_id")
        batch_op.drop_column("parent_agent_execution_id")
