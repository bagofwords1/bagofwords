"""repair_reports_and_llm_models

Revision ID: fa82623106ce
Revises: da68823779da
Create Date: 2025-11-16 12:39:39.648293

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import false


# revision identifiers, used by Alembic.
revision: str = 'fa82623106ce'
down_revision: Union[str, None] = 'da68823779da'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    # Repair reports.report_type and its index if missing
    if 'reports' in table_names:
        report_columns = {c['name'] for c in inspector.get_columns('reports')}
        existing_indexes = {i['name'] for i in inspector.get_indexes('reports')}

        # Add report_type column if it does not exist
        if 'report_type' not in report_columns:
            with op.batch_alter_table('reports', schema=None) as batch_op:
                batch_op.add_column(
                    sa.Column(
                        'report_type',
                        sa.String(),
                        nullable=False,
                        server_default='regular',
                    )
                )
                if 'ix_reports_report_type' not in existing_indexes:
                    batch_op.create_index(
                        op.f('ix_reports_report_type'),
                        ['report_type'],
                        unique=False,
                    )
        else:
            # Column exists but index might not
            if 'ix_reports_report_type' not in existing_indexes:
                with op.batch_alter_table('reports', schema=None) as batch_op:
                    batch_op.create_index(
                        op.f('ix_reports_report_type'),
                        ['report_type'],
                        unique=False,
                    )

    # Repair llm_models columns from 9479f7d83f08 if missing
    if 'llm_models' in table_names:
        llm_columns = {c['name'] for c in inspector.get_columns('llm_models')}

        # Only open a batch alter block if at least one column is missing
        missing_llm_cols = {
            name
            for name in [
                'is_small_default',
                'context_window_tokens',
                'max_output_tokens',
                'input_cost_per_million_tokens_usd',
                'output_cost_per_million_tokens_usd',
            ]
            if name not in llm_columns
        }

        if missing_llm_cols:
            with op.batch_alter_table('llm_models', schema=None) as batch_op:
                if 'is_small_default' in missing_llm_cols:
                    batch_op.add_column(
                        sa.Column(
                            'is_small_default',
                            sa.Boolean(),
                            nullable=False,
                            server_default=false(),
                        )
                    )
                if 'context_window_tokens' in missing_llm_cols:
                    batch_op.add_column(
                        sa.Column('context_window_tokens', sa.Integer(), nullable=True)
                    )
                if 'max_output_tokens' in missing_llm_cols:
                    batch_op.add_column(
                        sa.Column('max_output_tokens', sa.Integer(), nullable=True)
                    )
                if 'input_cost_per_million_tokens_usd' in missing_llm_cols:
                    batch_op.add_column(
                        sa.Column(
                            'input_cost_per_million_tokens_usd',
                            sa.Float(),
                            nullable=True,
                        )
                    )
                if 'output_cost_per_million_tokens_usd' in missing_llm_cols:
                    batch_op.add_column(
                        sa.Column(
                            'output_cost_per_million_tokens_usd',
                            sa.Float(),
                            nullable=True,
                        )
                    )


def downgrade() -> None:
    # No-op downgrade.
    # The original schema changes for reports.report_type and llm_models
    # are owned by earlier migrations (e.g. da68823779da and 9479f7d83f08),
    # so we delegate full rollback to those downgrades to avoid double-drops.
    pass
