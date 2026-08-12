"""add report history lookup indexes

Revision ID: rptperf002
Revises: rptperf001
Create Date: 2026-08-12 00:00:00.000000

Report instruction history joins builds by agent execution, then checks whether
an instruction/version pair exists in the main build. Without these indexes a
small report caused hundreds of thousands of nested-loop probes.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "rptperf002"
down_revision: str | None = "rptperf001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_agent_executions_report_id",
        "agent_executions",
        ["report_id"],
        unique=False,
    )
    op.create_index(
        "ix_instruction_builds_agent_execution_id",
        "instruction_builds",
        ["agent_execution_id"],
        unique=False,
    )
    op.create_index(
        "ix_build_contents_instruction_version_build",
        "build_contents",
        ["instruction_id", "instruction_version_id", "build_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_build_contents_instruction_version_build",
        table_name="build_contents",
    )
    op.drop_index(
        "ix_instruction_builds_agent_execution_id",
        table_name="instruction_builds",
    )
    op.drop_index("ix_agent_executions_report_id", table_name="agent_executions")
