"""agent execution and tools data models

Revision ID: 2c6238f56d1c
Revises: 8c1061a09336
Create Date: 2025-08-23 23:29:15.931489

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy import text
import uuid
from datetime import datetime

# revision identifiers, used by Alembic.
revision: str = '2c6238f56d1c'
down_revision: Union[str, None] = '8c1061a09336'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def backfill_agent_executions_for_completions():
    """Create AgentExecution records for existing system completions that don't have them."""
    connection = op.get_bind()
    
    # Historic version for all tracking completions
    historic_version = "0.0.189"
    current_time = datetime.utcnow()
    
    # Simple raw SQL query that works with both SQLite and PostgreSQL
    query = text("""
        SELECT c.id, c.report_id, c.user_id, c.status, c.created_at, c.updated_at,
               r.organization_id
        FROM completions c
        LEFT JOIN reports r ON c.report_id = r.id
        LEFT JOIN agent_executions ae ON c.id = ae.completion_id
        WHERE c.role = 'system' AND ae.id IS NULL
        ORDER BY c.created_at
    """)
    
    completions = connection.execute(query).fetchall()
    
    if not completions:
        print("No historical completions found that need agent executions.")
        return
        
    print(f"Backfilling {len(completions)} agent executions for historical completions...")
    
    # Insert agent executions in batches
    batch_size = 100
    for i in range(0, len(completions), batch_size):
        batch = completions[i:i + batch_size]
        values = []
        
        for comp in batch:
            # Calculate duration if both timestamps exist
            duration_ms = None
            if comp.created_at and comp.updated_at:
                try:
                    # Parse timestamp strings to datetime objects
                    created_at = datetime.fromisoformat(comp.created_at.replace('Z', '+00:00')) if isinstance(comp.created_at, str) else comp.created_at
                    updated_at = datetime.fromisoformat(comp.updated_at.replace('Z', '+00:00')) if isinstance(comp.updated_at, str) else comp.updated_at
                    duration_delta = updated_at - created_at
                    duration_ms = duration_delta.total_seconds() * 1000
                except (ValueError, TypeError):
                    # If timestamp parsing fails, just skip duration calculation
                    duration_ms = None
            
            # Map completion status to agent execution status
            ae_status = comp.status if comp.status in ['success', 'error', 'stopped'] else 'success'
            
            # Parse timestamps for insertion
            started_at = datetime.fromisoformat(comp.created_at.replace('Z', '+00:00')) if isinstance(comp.created_at, str) else comp.created_at
            completed_at = datetime.fromisoformat(comp.updated_at.replace('Z', '+00:00')) if isinstance(comp.updated_at, str) else comp.updated_at
            
            values.append({
                'id': str(uuid.uuid4()),
                'completion_id': comp.id,
                'organization_id': comp.organization_id,
                'user_id': comp.user_id,
                'report_id': comp.report_id,
                'status': ae_status,
                'started_at': started_at,
                'completed_at': completed_at,
                'total_duration_ms': duration_ms,
                'latest_seq': 0,
                'bow_version': historic_version,
                'config_json': '{"backfilled": true}',
                'created_at': current_time,
                'updated_at': current_time
            })
        
        # Insert batch using bulk insert (database-agnostic)
        if values:  # Only insert if we have values
            # Use bulk insert with text() for better compatibility
            placeholders = []
            for val in values:
                org_id = 'NULL' if val['organization_id'] is None else f"'{val['organization_id']}'"
                user_id = 'NULL' if val['user_id'] is None else f"'{val['user_id']}'"
                report_id = 'NULL' if val['report_id'] is None else f"'{val['report_id']}'"
                started_at = 'NULL' if val['started_at'] is None else f"'{val['started_at']}'"
                completed_at = 'NULL' if val['completed_at'] is None else f"'{val['completed_at']}'"
                duration_ms = 'NULL' if val['total_duration_ms'] is None else str(val['total_duration_ms'])
                
                placeholder = (
                    f"('{val['id']}', '{val['completion_id']}', "
                    f"{org_id}, {user_id}, {report_id}, "
                    f"'{val['status']}', {started_at}, {completed_at}, "
                    f"{duration_ms}, {val['latest_seq']}, '{val['bow_version']}', "
                    f"'{val['config_json']}', '{val['created_at']}', '{val['updated_at']}')"
                )
                placeholders.append(placeholder)
            
            placeholders_str = ', '.join(placeholders)
            
            insert_sql = text(f"""
                INSERT INTO agent_executions (
                    id, completion_id, organization_id, user_id, report_id,
                    status, started_at, completed_at, total_duration_ms, 
                    latest_seq, bow_version, config_json, created_at, updated_at
                ) VALUES {placeholders_str}
            """)
            
            connection.execute(insert_sql)
        print(f"Inserted agent executions for batch {i//batch_size + 1}/{(len(completions) + batch_size - 1)//batch_size}")
    
    print(f"Backfill complete! Created agent executions for {len(completions)} historical completions with version '{historic_version}'")


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('agent_executions',
    sa.Column('completion_id', sa.String(length=36), nullable=False),
    sa.Column('organization_id', sa.String(length=36), nullable=True),
    sa.Column('user_id', sa.String(length=36), nullable=True),
    sa.Column('report_id', sa.String(length=36), nullable=True),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('total_duration_ms', sa.Float(), nullable=True),
    sa.Column('first_token_ms', sa.Float(), nullable=True),
    sa.Column('thinking_ms', sa.Float(), nullable=True),
    sa.Column('latest_seq', sa.Integer(), nullable=False),
    sa.Column('token_usage_json', sa.JSON(), nullable=True),
    sa.Column('error_json', sa.JSON(), nullable=True),
    sa.Column('config_json', sa.JSON(), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('bow_version', sa.String(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['completion_id'], ['completions.id'], ),
    sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('agent_executions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_agent_executions_completion_id'), ['completion_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_agent_executions_id'), ['id'], unique=True)

    op.create_table('context_snapshots',
    sa.Column('agent_execution_id', sa.String(length=36), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('context_view_json', sa.JSON(), nullable=False),
    sa.Column('prompt_text', sa.String(), nullable=True),
    sa.Column('prompt_tokens', sa.String(), nullable=True),
    sa.Column('hash', sa.String(), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['agent_execution_id'], ['agent_executions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('context_snapshots', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_context_snapshots_agent_execution_id'), ['agent_execution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_context_snapshots_id'), ['id'], unique=True)

    op.create_table('plan_decisions',
    sa.Column('agent_execution_id', sa.String(length=36), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('loop_index', sa.Integer(), nullable=False),
    sa.Column('plan_type', sa.String(), nullable=True),
    sa.Column('analysis_complete', sa.Boolean(), nullable=False),
    sa.Column('reasoning', sa.String(), nullable=True),
    sa.Column('assistant', sa.String(), nullable=True),
    sa.Column('final_answer', sa.String(), nullable=True),
    sa.Column('action_name', sa.String(), nullable=True),
    sa.Column('action_args_json', sa.JSON(), nullable=True),
    sa.Column('metrics_json', sa.JSON(), nullable=True),
    sa.Column('context_snapshot_id', sa.String(length=36), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['agent_execution_id'], ['agent_executions.id'], ),
    sa.ForeignKeyConstraint(['context_snapshot_id'], ['context_snapshots.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('plan_decisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_plan_decisions_agent_execution_id'), ['agent_execution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_plan_decisions_id'), ['id'], unique=True)
        batch_op.create_unique_constraint('uq_plan_decisions_execution_seq', ['agent_execution_id', 'seq'])

    op.create_table('tool_executions',
    sa.Column('agent_execution_id', sa.String(length=36), nullable=False),
    sa.Column('plan_decision_id', sa.String(length=36), nullable=True),
    sa.Column('tool_name', sa.String(), nullable=False),
    sa.Column('tool_action', sa.String(), nullable=True),
    sa.Column('arguments_json', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('success', sa.Boolean(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.Column('duration_ms', sa.Float(), nullable=True),
    sa.Column('attempt_number', sa.Integer(), nullable=False),
    sa.Column('max_retries', sa.Integer(), nullable=False),
    sa.Column('token_usage_json', sa.JSON(), nullable=True),
    sa.Column('result_summary', sa.String(), nullable=True),
    sa.Column('result_json', sa.JSON(), nullable=True),
    sa.Column('artifact_refs_json', sa.JSON(), nullable=True),
    sa.Column('created_widget_id', sa.String(length=36), nullable=True),
    sa.Column('created_step_id', sa.String(length=36), nullable=True),
    sa.Column('context_snapshot_id', sa.String(length=36), nullable=True),
    sa.Column('error_message', sa.String(), nullable=True),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['agent_execution_id'], ['agent_executions.id'], ),
    sa.ForeignKeyConstraint(['context_snapshot_id'], ['context_snapshots.id'], ),
    sa.ForeignKeyConstraint(['created_step_id'], ['steps.id'], ),
    sa.ForeignKeyConstraint(['created_widget_id'], ['widgets.id'], ),
    sa.ForeignKeyConstraint(['plan_decision_id'], ['plan_decisions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('tool_executions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_tool_executions_agent_execution_id'), ['agent_execution_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_tool_executions_id'), ['id'], unique=True)

    # Add index for bow_version for performance
    with op.batch_alter_table('agent_executions', schema=None) as batch_op:
        batch_op.create_index('ix_agent_executions_bow_version', ['bow_version'], unique=False)

    # Backfill: Create AgentExecution records for historical system completions
    backfill_agent_executions_for_completions()

    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
  
    with op.batch_alter_table('tool_executions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_tool_executions_id'))
        batch_op.drop_index(batch_op.f('ix_tool_executions_agent_execution_id'))

    op.drop_table('tool_executions')
    with op.batch_alter_table('plan_decisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_plan_decisions_id'))
        batch_op.drop_index(batch_op.f('ix_plan_decisions_agent_execution_id'))

    op.drop_table('plan_decisions')
    with op.batch_alter_table('context_snapshots', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_context_snapshots_id'))
        batch_op.drop_index(batch_op.f('ix_context_snapshots_agent_execution_id'))

    op.drop_table('context_snapshots')
    with op.batch_alter_table('agent_executions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_agent_executions_id'))
        batch_op.drop_index(batch_op.f('ix_agent_executions_completion_id'))

    op.drop_table('agent_executions')
    # ### end Alembic commands ###
