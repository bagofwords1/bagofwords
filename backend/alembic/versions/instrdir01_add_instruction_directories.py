"""add instruction directories and placements

Cosmetic, per-agent folders for organizing instructions in the KnowledgeExplorer
tree. Placement lives on the (instruction, directory) edge so an instruction's
folder for one agent never affects another agent (instructions are m:n with
agents). No semantics — the AI context is unaffected.

Revision ID: instrdir01
Revises: sk1c2t3a4l5g
Create Date: 2026-08-01
"""
from alembic import op
import sqlalchemy as sa


revision = 'instrdir01'
down_revision = 'sk1c2t3a4l5g'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'instruction_directories',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('data_source_id', sa.String(length=36), nullable=True),
        sa.Column('parent_id', sa.String(length=36), nullable=True),
        sa.Column('name', sa.String(length=100), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_by_user_id', sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
        sa.ForeignKeyConstraint(['data_source_id'], ['data_sources.id'], ),
        sa.ForeignKeyConstraint(['parent_id'], ['instruction_directories.id'], ),
        sa.ForeignKeyConstraint(['created_by_user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_instruction_directories_id', 'instruction_directories', ['id'], unique=False)
    op.create_index('ix_instruction_directories_scope', 'instruction_directories', ['organization_id', 'data_source_id'], unique=False)

    op.create_table(
        'instruction_directory_placements',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.Column('instruction_id', sa.String(length=36), nullable=False),
        sa.Column('directory_id', sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(['instruction_id'], ['instructions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['directory_id'], ['instruction_directories.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('instruction_id', 'directory_id', name='uq_instruction_directory_placement'),
    )
    op.create_index('ix_instruction_directory_placements_id', 'instruction_directory_placements', ['id'], unique=False)
    op.create_index('ix_instruction_directory_placements_directory', 'instruction_directory_placements', ['directory_id'], unique=False)
    op.create_index('ix_instruction_directory_placements_instruction', 'instruction_directory_placements', ['instruction_id'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_instruction_directory_placements_instruction', table_name='instruction_directory_placements')
    op.drop_index('ix_instruction_directory_placements_directory', table_name='instruction_directory_placements')
    op.drop_index('ix_instruction_directory_placements_id', table_name='instruction_directory_placements')
    op.drop_table('instruction_directory_placements')
    op.drop_index('ix_instruction_directories_scope', table_name='instruction_directories')
    op.drop_index('ix_instruction_directories_id', table_name='instruction_directories')
    op.drop_table('instruction_directories')
