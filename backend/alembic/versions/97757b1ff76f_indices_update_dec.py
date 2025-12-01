"""indices update dec

Revision ID: 97757b1ff76f
Revises: 52668a7dca6d
Create Date: 2025-12-01 16:07:24.293124

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '97757b1ff76f'
down_revision: Union[str, None] = '52668a7dca6d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_index_if_not_exists(index_name: str, table_name: str, columns: list, **kwargs):
    """Create index only if it doesn't already exist."""
    try:
        op.create_index(index_name, table_name, columns, **kwargs)
    except Exception:
        # Index already exists, skip
        pass


def _drop_index_if_exists(index_name: str, table_name: str):
    """Drop index only if it exists."""
    try:
        op.drop_index(index_name, table_name)
    except Exception:
        # Index doesn't exist, skip
        pass


def upgrade() -> None:
    # ===========================================
    # PHASE 1: Critical - Completion Flow
    # ===========================================
    
    # Completion table - MOST IMPORTANT for all completion queries
    _create_index_if_not_exists('ix_completions_report_id', 'completions', ['report_id'])
    _create_index_if_not_exists('ix_completions_parent_id', 'completions', ['parent_id'])
    _create_index_if_not_exists('ix_completions_report_created', 'completions', ['report_id', sa.text('created_at DESC')])
    
    # Widget - for report widget queries
    _create_index_if_not_exists('ix_widgets_report_id', 'widgets', ['report_id'])
    
    # Step - for widget step queries and query step lookups
    _create_index_if_not_exists('ix_steps_widget_id', 'steps', ['widget_id'])
    # Note: ix_steps_query_id already exists from migration b05d701bf229
    
    # ===========================================
    # PHASE 2: Important - Instructions & Metadata
    # ===========================================
    
    # Instruction table - for instruction queries
    _create_index_if_not_exists('ix_instructions_org_id', 'instructions', ['organization_id'])
    _create_index_if_not_exists('ix_instructions_agent_exec_id', 'instructions', ['agent_execution_id'])
    _create_index_if_not_exists('ix_instructions_user_id', 'instructions', ['user_id'])
    
    # DataSourceTable - for schema queries
    _create_index_if_not_exists('ix_datasource_tables_ds_id', 'datasource_tables', ['datasource_id'])
    
    # MetadataResource - for resource queries
    _create_index_if_not_exists('ix_metadata_resources_ds_id', 'metadata_resources', ['data_source_id'])
    _create_index_if_not_exists('ix_metadata_resources_type', 'metadata_resources', ['resource_type'])
    
    # ===========================================
    # PHASE 3: Supporting - Data Sources & Jobs
    # ===========================================
    
    # DataSource - for org data source queries
    _create_index_if_not_exists('ix_data_sources_org_id', 'data_sources', ['organization_id'])
    
    # MetadataIndexingJob - for job queries
    _create_index_if_not_exists('ix_metadata_jobs_ds_id', 'metadata_indexing_jobs', ['data_source_id'])
    _create_index_if_not_exists('ix_metadata_jobs_org_id', 'metadata_indexing_jobs', ['organization_id'])
    
    # GitRepository - for repo lookups
    _create_index_if_not_exists('ix_git_repositories_ds_id', 'git_repositories', ['data_source_id'])
    
    # Plan - for completion plan queries
    _create_index_if_not_exists('ix_plans_completion_id', 'plans', ['completion_id'])
    
    # ===========================================
    # PHASE 4: Dashboard - Layout & Text Widgets
    # ===========================================
    
    # DashboardLayoutVersion - for layout queries by report (used heavily by frontend)
    # Queries: get_layouts_for_report, _get_active_layout, set_active_layout, remove_blocks_for_text_widget
    _create_index_if_not_exists('ix_dashboard_layouts_report_id', 'dashboard_layout_versions', ['report_id'])
    # Composite index for active layout lookup (report_id + is_active)
    _create_index_if_not_exists('ix_dashboard_layouts_report_active', 'dashboard_layout_versions', ['report_id', 'is_active'])
    
    # TextWidget - for text widget queries by report
    # Queries: loadTextWidgetsForReport, hydrate in get_layouts_for_report
    _create_index_if_not_exists('ix_text_widgets_report_id', 'text_widgets', ['report_id'])

    # ===========================================
    # PHASE 5: Feedback & Mentions
    # ===========================================
    
    # CompletionFeedback - CRITICAL for batch-loading user feedback (avoids N+1 API calls)
    # Queries: batch-load in get_completions_v2, get_feedback_summary, create_or_update_feedback
    _create_index_if_not_exists('ix_completion_feedbacks_completion_id', 'completion_feedbacks', ['completion_id'])
    _create_index_if_not_exists('ix_completion_feedbacks_user_id', 'completion_feedbacks', ['user_id'])
    _create_index_if_not_exists('ix_completion_feedbacks_org_id', 'completion_feedbacks', ['organization_id'])
    # Composite for unique user feedback lookup (completion_id + user_id + organization_id)
    _create_index_if_not_exists('ix_completion_feedbacks_lookup', 'completion_feedbacks', ['completion_id', 'user_id', 'organization_id'])
    
    # Mention - for completion and report scoped queries
    # Queries: get_memories, create_completion_mentions
    _create_index_if_not_exists('ix_mentions_completion_id', 'mentions', ['completion_id'])
    _create_index_if_not_exists('ix_mentions_report_id', 'mentions', ['report_id'])
    _create_index_if_not_exists('ix_mentions_completion_type', 'mentions', ['completion_id', 'type'])


def downgrade() -> None:
    # Drop all indexes in reverse order (only those created by this migration)
    
    # Phase 5
    _drop_index_if_exists('ix_mentions_completion_type', 'mentions')
    _drop_index_if_exists('ix_mentions_report_id', 'mentions')
    _drop_index_if_exists('ix_mentions_completion_id', 'mentions')
    _drop_index_if_exists('ix_completion_feedbacks_lookup', 'completion_feedbacks')
    _drop_index_if_exists('ix_completion_feedbacks_org_id', 'completion_feedbacks')
    _drop_index_if_exists('ix_completion_feedbacks_user_id', 'completion_feedbacks')
    _drop_index_if_exists('ix_completion_feedbacks_completion_id', 'completion_feedbacks')
    
    # Phase 4
    _drop_index_if_exists('ix_text_widgets_report_id', 'text_widgets')
    _drop_index_if_exists('ix_dashboard_layouts_report_active', 'dashboard_layout_versions')
    _drop_index_if_exists('ix_dashboard_layouts_report_id', 'dashboard_layout_versions')
    
    # Phase 3
    _drop_index_if_exists('ix_plans_completion_id', 'plans')
    _drop_index_if_exists('ix_git_repositories_ds_id', 'git_repositories')
    _drop_index_if_exists('ix_metadata_jobs_org_id', 'metadata_indexing_jobs')
    _drop_index_if_exists('ix_metadata_jobs_ds_id', 'metadata_indexing_jobs')
    _drop_index_if_exists('ix_data_sources_org_id', 'data_sources')
    
    # Phase 2
    _drop_index_if_exists('ix_metadata_resources_type', 'metadata_resources')
    _drop_index_if_exists('ix_metadata_resources_ds_id', 'metadata_resources')
    _drop_index_if_exists('ix_datasource_tables_ds_id', 'datasource_tables')
    _drop_index_if_exists('ix_instructions_user_id', 'instructions')
    _drop_index_if_exists('ix_instructions_agent_exec_id', 'instructions')
    _drop_index_if_exists('ix_instructions_org_id', 'instructions')
    
    # Phase 1
    # Note: ix_steps_query_id is NOT dropped here as it was created by another migration
    _drop_index_if_exists('ix_steps_widget_id', 'steps')
    _drop_index_if_exists('ix_widgets_report_id', 'widgets')
    _drop_index_if_exists('ix_completions_report_created', 'completions')
    _drop_index_if_exists('ix_completions_parent_id', 'completions')
    _drop_index_if_exists('ix_completions_report_id', 'completions')
