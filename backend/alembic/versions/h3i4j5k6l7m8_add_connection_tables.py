"""add connection tables for domain-connection architecture

Revision ID: h3i4j5k6l7m8
Revises: g2h3i4j5k6l7
Create Date: 2024-12-30 12:00:00.000000

Introduces Connection model with M:N relationship to DataSource (Domain).
Adds ConnectionTable for schema storage and UserConnectionCredentials/UserConnectionTable
for per-user authentication at the connection level.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'h3i4j5k6l7m8'
down_revision: Union[str, None] = 'c07deb3aeac8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Create connections table
    op.create_table('connections',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False),
        sa.Column('credentials', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('last_synced_at', sa.DateTime(), nullable=True),
        sa.Column('auth_policy', sa.String(), nullable=False, server_default=sa.text("'system_only'")),
        sa.Column('allowed_user_auth_modes', sa.JSON(), nullable=True),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_connections_id'), ['id'], unique=True)
        batch_op.create_index(batch_op.f('ix_connections_organization_id'), ['organization_id'], unique=False)

    # 2. Create connection_tables table
    op.create_table('connection_tables',
        sa.Column('name', sa.String(), nullable=False),
        sa.Column('connection_id', sa.String(length=36), nullable=False),
        sa.Column('columns', sa.JSON(), nullable=False),
        sa.Column('pks', sa.JSON(), nullable=False),
        sa.Column('fks', sa.JSON(), nullable=False),
        sa.Column('no_rows', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('centrality_score', sa.Float(), nullable=True),
        sa.Column('richness', sa.Float(), nullable=True),
        sa.Column('degree_in', sa.Integer(), nullable=True),
        sa.Column('degree_out', sa.Integer(), nullable=True),
        sa.Column('entity_like', sa.Boolean(), nullable=True),
        sa.Column('metrics_computed_at', sa.DateTime(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('connection_tables', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_connection_tables_id'), ['id'], unique=True)
        batch_op.create_index(batch_op.f('ix_connection_tables_connection_id'), ['connection_id'], unique=False)

    # 3. Create domain_connection junction table (M:N between DataSource and Connection)
    op.create_table('domain_connection',
        sa.Column('data_source_id', sa.String(length=36), nullable=False),
        sa.Column('connection_id', sa.String(length=36), nullable=False),
        sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['data_source_id'], ['data_sources.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('data_source_id', 'connection_id')
    )

    # 4. Create user_connection_credentials table
    op.create_table('user_connection_credentials',
        sa.Column('connection_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('organization_id', sa.String(length=36), nullable=False),
        sa.Column('auth_mode', sa.String(length=64), nullable=False),
        sa.Column('encrypted_credentials', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('is_primary', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=True),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ),
        sa.ForeignKeyConstraint(['organization_id'], ['organizations.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('user_connection_credentials', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_connection_credentials_id'), ['id'], unique=True)
        batch_op.create_index(batch_op.f('ix_user_connection_credentials_connection_id'), ['connection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_connection_credentials_user_id'), ['user_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_connection_credentials_organization_id'), ['organization_id'], unique=False)

    # 5. Create user_connection_tables table
    op.create_table('user_connection_tables',
        sa.Column('connection_id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('table_name', sa.String(), nullable=False),
        sa.Column('connection_table_id', sa.String(length=36), nullable=True),
        sa.Column('is_accessible', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('status', sa.String(), nullable=False, server_default=sa.text("'accessible'")),
        sa.Column('metadata_json', sa.JSON(), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ),
        sa.ForeignKeyConstraint(['connection_table_id'], ['connection_tables.id'], ),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('connection_id', 'user_id', 'table_name', name='uq_user_conn_table')
    )
    with op.batch_alter_table('user_connection_tables', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_connection_tables_id'), ['id'], unique=True)
        batch_op.create_index(batch_op.f('ix_user_connection_tables_connection_id'), ['connection_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_connection_tables_user_id'), ['user_id'], unique=False)

    # 6. Create user_connection_columns table
    op.create_table('user_connection_columns',
        sa.Column('user_connection_table_id', sa.String(length=36), nullable=False),
        sa.Column('column_name', sa.String(), nullable=False),
        sa.Column('is_accessible', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('is_masked', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('data_type', sa.String(), nullable=True),
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('deleted_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['user_connection_table_id'], ['user_connection_tables.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('user_connection_table_id', 'column_name', name='uq_user_conn_table_column')
    )
    with op.batch_alter_table('user_connection_columns', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_connection_columns_id'), ['id'], unique=True)
        batch_op.create_index(batch_op.f('ix_user_connection_columns_user_connection_table_id'), ['user_connection_table_id'], unique=False)

    # 7. Add connection_table_id to datasource_tables (nullable for backward compatibility)
    with op.batch_alter_table('datasource_tables', schema=None) as batch_op:
        batch_op.add_column(sa.Column('connection_table_id', sa.String(length=36), nullable=True))
        batch_op.create_index(batch_op.f('ix_datasource_tables_connection_table_id'), ['connection_table_id'], unique=False)
        batch_op.create_foreign_key('fk_datasource_tables_connection_table_id', 'connection_tables', ['connection_table_id'], ['id'])

    # 8. Make legacy columns nullable in datasource_tables for migration period
    with op.batch_alter_table('datasource_tables', schema=None) as batch_op:
        batch_op.alter_column('columns', existing_type=sa.JSON(), nullable=True)
        batch_op.alter_column('pks', existing_type=sa.JSON(), nullable=True)
        batch_op.alter_column('fks', existing_type=sa.JSON(), nullable=True)
        batch_op.alter_column('no_rows', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # Revert datasource_tables changes
    with op.batch_alter_table('datasource_tables', schema=None) as batch_op:
        batch_op.alter_column('no_rows', existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column('fks', existing_type=sa.JSON(), nullable=False)
        batch_op.alter_column('pks', existing_type=sa.JSON(), nullable=False)
        batch_op.alter_column('columns', existing_type=sa.JSON(), nullable=False)
        batch_op.drop_constraint('fk_datasource_tables_connection_table_id', type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_datasource_tables_connection_table_id'))
        batch_op.drop_column('connection_table_id')

    # Drop user_connection_columns
    with op.batch_alter_table('user_connection_columns', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_connection_columns_user_connection_table_id'))
        batch_op.drop_index(batch_op.f('ix_user_connection_columns_id'))
    op.drop_table('user_connection_columns')

    # Drop user_connection_tables
    with op.batch_alter_table('user_connection_tables', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_connection_tables_user_id'))
        batch_op.drop_index(batch_op.f('ix_user_connection_tables_connection_id'))
        batch_op.drop_index(batch_op.f('ix_user_connection_tables_id'))
    op.drop_table('user_connection_tables')

    # Drop user_connection_credentials
    with op.batch_alter_table('user_connection_credentials', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_connection_credentials_organization_id'))
        batch_op.drop_index(batch_op.f('ix_user_connection_credentials_user_id'))
        batch_op.drop_index(batch_op.f('ix_user_connection_credentials_connection_id'))
        batch_op.drop_index(batch_op.f('ix_user_connection_credentials_id'))
    op.drop_table('user_connection_credentials')

    # Drop domain_connection junction table
    op.drop_table('domain_connection')

    # Drop connection_tables
    with op.batch_alter_table('connection_tables', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_connection_tables_connection_id'))
        batch_op.drop_index(batch_op.f('ix_connection_tables_id'))
    op.drop_table('connection_tables')

    # Drop connections
    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_connections_organization_id'))
        batch_op.drop_index(batch_op.f('ix_connections_id'))
    op.drop_table('connections')

