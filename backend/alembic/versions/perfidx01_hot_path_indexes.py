"""hot-path perf indexes (monitoring, list pages, rbac association)

Adds composite indexes matching the hot filter/sort predicates on the pages
that dominate browsing (agents, dashboards, monitoring, prompts, queries):

  * Monitoring/console aggregates filter (organization_id, created_at) on
    agent_executions and llm_usage_records, and fan out IN(agent_execution_id)
    over tool_executions — none of those columns were indexed, so every
    time-ranged aggregate seq-scanned the whole table.
  * List endpoints sort/filter on (organization_id, created_at/updated_at):
    reports, prompts, entities. Report.organization_id was indexed but not with
    created_at; prompts.organization_id was not indexed at all.
  * scheduled_prompts filtered by user_id (filter='my') with no index.
  * The RBAC table-accessibility + per-agent count paths filter
    instruction_references.instruction_id and
    instruction_data_source_association.data_source_id (the 2nd column of its
    composite PK, so unusable as a seek prefix) — both unindexed.

All indexes are additive and idempotent; query behavior is unchanged.

Revision ID: perfidx01
Revises: pendsweep01
Create Date: 2026-07-01
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "perfidx01"
down_revision: Union[str, None] = "pendsweep01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_INDEXES = [
    # monitoring / console time-ranged aggregates
    ("ix_ae_org_created", "agent_executions", ["organization_id", "created_at"]),
    ("ix_llm_usage_org_created", "llm_usage_records", ["organization_id", "created_at"]),
    ("ix_completions_report_created", "completions", ["report_id", "created_at"]),
    ("ix_tool_exec_ae_success", "tool_executions", ["agent_execution_id", "success"]),
    # list pages: filter org + sort by time
    ("ix_reports_org_created", "reports", ["organization_id", "created_at"]),
    ("ix_prompts_org_created", "prompts", ["organization_id", "created_at"]),
    ("ix_entities_org_updated", "entities", ["organization_id", "updated_at"]),
    ("ix_scheduled_prompts_user", "scheduled_prompts", ["user_id"]),
    # rbac / instruction hot lookups
    ("ix_instr_refs_instr_obj", "instruction_references", ["instruction_id", "object_type"]),
    ("ix_idsa_data_source_id", "instruction_data_source_association", ["data_source_id"]),
]


def _existing_indexes(inspector, table_name):
    try:
        return {ix["name"] for ix in inspector.get_indexes(table_name)}
    except Exception:
        return set()


def upgrade() -> None:
    # Check existence up front so no create_index ever raises — a failed
    # statement would abort the whole migration transaction in Postgres.
    inspector = sa.inspect(op.get_bind())
    for name, table, cols in _INDEXES:
        if name not in _existing_indexes(inspector, table):
            op.create_index(name, table, cols)


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    for name, table, cols in reversed(_INDEXES):
        if name in _existing_indexes(inspector, table):
            op.drop_index(name, table)
