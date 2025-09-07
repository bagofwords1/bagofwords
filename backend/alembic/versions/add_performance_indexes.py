"""Add performance indexes

Revision ID: performance_indexes_001
Revises: 
Create Date: 2025-01-07 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'performance_indexes_001'
down_revision = None
depends_on = None

def upgrade():
    # Add indexes for frequently queried columns
    
    # Completion table indexes
    op.create_index('idx_completion_report_id', 'completion', ['report_id'])
    op.create_index('idx_completion_user_id', 'completion', ['user_id'])
    op.create_index('idx_completion_created_at', 'completion', ['created_at'])
    op.create_index('idx_completion_status', 'completion', ['status'])
    op.create_index('idx_completion_role', 'completion', ['role'])
    op.create_index('idx_completion_parent_id', 'completion', ['parent_id'])
    
    # Report table indexes
    op.create_index('idx_report_organization_id', 'report', ['organization_id'])
    op.create_index('idx_report_user_id', 'report', ['user_id'])
    op.create_index('idx_report_created_at', 'report', ['created_at'])
    op.create_index('idx_report_updated_at', 'report', ['updated_at'])
    
    # Widget table indexes
    op.create_index('idx_widget_report_id', 'widget', ['report_id'])
    op.create_index('idx_widget_created_at', 'widget', ['created_at'])
    
    # Step table indexes
    op.create_index('idx_step_widget_id', 'step', ['widget_id'])
    op.create_index('idx_step_status', 'step', ['status'])
    op.create_index('idx_step_created_at', 'step', ['created_at'])
    
    # Data source table indexes
    op.create_index('idx_data_source_organization_id', 'data_source', ['organization_id'])
    op.create_index('idx_data_source_type', 'data_source', ['type'])
    op.create_index('idx_data_source_is_active', 'data_source', ['is_active'])
    
    # User table indexes
    op.create_index('idx_user_email', 'user', ['email'])
    op.create_index('idx_user_is_active', 'user', ['is_active'])
    op.create_index('idx_user_is_verified', 'user', ['is_verified'])
    
    # Organization table indexes
    op.create_index('idx_organization_created_at', 'organization', ['created_at'])
    
    # Membership table indexes
    op.create_index('idx_membership_user_id', 'membership', ['user_id'])
    op.create_index('idx_membership_organization_id', 'membership', ['organization_id'])
    op.create_index('idx_membership_role', 'membership', ['role'])
    
    # Agent execution table indexes
    op.create_index('idx_agent_execution_completion_id', 'agent_execution', ['completion_id'])
    op.create_index('idx_agent_execution_status', 'agent_execution', ['status'])
    op.create_index('idx_agent_execution_created_at', 'agent_execution', ['created_at'])
    
    # Completion block table indexes
    op.create_index('idx_completion_block_completion_id', 'completion_block', ['completion_id'])
    op.create_index('idx_completion_block_block_index', 'completion_block', ['block_index'])
    op.create_index('idx_completion_block_seq', 'completion_block', ['seq'])
    
    # Tool execution table indexes
    op.create_index('idx_tool_execution_agent_execution_id', 'tool_execution', ['agent_execution_id'])
    op.create_index('idx_tool_execution_tool_name', 'tool_execution', ['tool_name'])
    op.create_index('idx_tool_execution_status', 'tool_execution', ['status'])
    
    # File table indexes
    op.create_index('idx_file_organization_id', 'file', ['organization_id'])
    op.create_index('idx_file_user_id', 'file', ['user_id'])
    op.create_index('idx_file_created_at', 'file', ['created_at'])
    
    # Memory table indexes
    op.create_index('idx_memory_organization_id', 'memory', ['organization_id'])
    op.create_index('idx_memory_user_id', 'memory', ['user_id'])
    op.create_index('idx_memory_created_at', 'memory', ['created_at'])

def downgrade():
    # Drop all the indexes
    op.drop_index('idx_completion_report_id')
    op.drop_index('idx_completion_user_id')
    op.drop_index('idx_completion_created_at')
    op.drop_index('idx_completion_status')
    op.drop_index('idx_completion_role')
    op.drop_index('idx_completion_parent_id')
    
    op.drop_index('idx_report_organization_id')
    op.drop_index('idx_report_user_id')
    op.drop_index('idx_report_created_at')
    op.drop_index('idx_report_updated_at')
    
    op.drop_index('idx_widget_report_id')
    op.drop_index('idx_widget_created_at')
    
    op.drop_index('idx_step_widget_id')
    op.drop_index('idx_step_status')
    op.drop_index('idx_step_created_at')
    
    op.drop_index('idx_data_source_organization_id')
    op.drop_index('idx_data_source_type')
    op.drop_index('idx_data_source_is_active')
    
    op.drop_index('idx_user_email')
    op.drop_index('idx_user_is_active')
    op.drop_index('idx_user_is_verified')
    
    op.drop_index('idx_organization_created_at')
    
    op.drop_index('idx_membership_user_id')
    op.drop_index('idx_membership_organization_id')
    op.drop_index('idx_membership_role')
    
    op.drop_index('idx_agent_execution_completion_id')
    op.drop_index('idx_agent_execution_status')
    op.drop_index('idx_agent_execution_created_at')
    
    op.drop_index('idx_completion_block_completion_id')
    op.drop_index('idx_completion_block_block_index')
    op.drop_index('idx_completion_block_seq')
    
    op.drop_index('idx_tool_execution_agent_execution_id')
    op.drop_index('idx_tool_execution_tool_name')
    op.drop_index('idx_tool_execution_status')
    
    op.drop_index('idx_file_organization_id')
    op.drop_index('idx_file_user_id')
    op.drop_index('idx_file_created_at')
    
    op.drop_index('idx_memory_organization_id')
    op.drop_index('idx_memory_user_id')
    op.drop_index('idx_memory_created_at')