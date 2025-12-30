"""data migration for connections

Revision ID: i4j5k6l7m8n9
Revises: h3i4j5k6l7m8
Create Date: 2024-12-30 12:01:00.000000

Migrates existing data from data_sources to connections:
1. For each DataSource, create a Connection with extracted fields
2. Insert into domain_connection junction table
3. For each DataSourceTable, create a ConnectionTable and link
4. Migrate user credentials and overlays
"""
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.orm import Session


# revision identifiers, used by Alembic.
revision: str = 'i4j5k6l7m8n9'
down_revision: Union[str, None] = 'h3i4j5k6l7m8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    session = Session(bind=bind)
    
    try:
        # 1. Get all existing data sources
        data_sources = session.execute(
            sa.text("""
                SELECT id, name, type, config, credentials, is_active, last_synced_at, 
                       auth_policy, allowed_user_auth_modes, organization_id,
                       created_at, updated_at
                FROM data_sources
                WHERE deleted_at IS NULL
            """)
        ).fetchall()
        
        # Map from data_source_id to connection_id
        ds_to_conn = {}
        
        for ds in data_sources:
            # 2. Create a Connection for each DataSource
            conn_id = str(uuid.uuid4())
            ds_to_conn[ds.id] = conn_id
            
            session.execute(
                sa.text("""
                    INSERT INTO connections (
                        id, name, type, config, credentials, is_active, last_synced_at,
                        auth_policy, allowed_user_auth_modes, organization_id,
                        created_at, updated_at
                    ) VALUES (
                        :id, :name, :type, :config, :credentials, :is_active, :last_synced_at,
                        :auth_policy, :allowed_user_auth_modes, :organization_id,
                        :created_at, :updated_at
                    )
                """),
                {
                    'id': conn_id,
                    'name': ds.name,
                    'type': ds.type,
                    'config': ds.config,
                    'credentials': ds.credentials,
                    'is_active': ds.is_active,
                    'last_synced_at': ds.last_synced_at,
                    'auth_policy': ds.auth_policy or 'system_only',
                    'allowed_user_auth_modes': ds.allowed_user_auth_modes,
                    'organization_id': ds.organization_id,
                    'created_at': ds.created_at,
                    'updated_at': ds.updated_at,
                }
            )
            
            # 3. Insert into domain_connection junction table
            session.execute(
                sa.text("""
                    INSERT INTO domain_connection (data_source_id, connection_id)
                    VALUES (:data_source_id, :connection_id)
                """),
                {
                    'data_source_id': ds.id,
                    'connection_id': conn_id,
                }
            )
        
        # 4. For each DataSourceTable, create ConnectionTable and link
        datasource_tables = session.execute(
            sa.text("""
                SELECT id, name, datasource_id, columns, pks, fks, no_rows,
                       centrality_score, richness, degree_in, degree_out, 
                       entity_like, metrics_computed_at, metadata_json,
                       created_at, updated_at
                FROM datasource_tables
                WHERE deleted_at IS NULL
            """)
        ).fetchall()
        
        for dst in datasource_tables:
            if dst.datasource_id not in ds_to_conn:
                # Data source was deleted or not found
                continue
                
            conn_id = ds_to_conn[dst.datasource_id]
            conn_table_id = str(uuid.uuid4())
            
            # Create ConnectionTable with schema fields
            session.execute(
                sa.text("""
                    INSERT INTO connection_tables (
                        id, name, connection_id, columns, pks, fks, no_rows,
                        centrality_score, richness, degree_in, degree_out,
                        entity_like, metrics_computed_at, metadata_json,
                        created_at, updated_at
                    ) VALUES (
                        :id, :name, :connection_id, :columns, :pks, :fks, :no_rows,
                        :centrality_score, :richness, :degree_in, :degree_out,
                        :entity_like, :metrics_computed_at, :metadata_json,
                        :created_at, :updated_at
                    )
                """),
                {
                    'id': conn_table_id,
                    'name': dst.name,
                    'connection_id': conn_id,
                    'columns': dst.columns,
                    'pks': dst.pks,
                    'fks': dst.fks,
                    'no_rows': dst.no_rows or 0,
                    'centrality_score': dst.centrality_score,
                    'richness': dst.richness,
                    'degree_in': dst.degree_in,
                    'degree_out': dst.degree_out,
                    'entity_like': dst.entity_like,
                    'metrics_computed_at': dst.metrics_computed_at,
                    'metadata_json': dst.metadata_json,
                    'created_at': dst.created_at,
                    'updated_at': dst.updated_at,
                }
            )
            
            # Update DataSourceTable with connection_table_id link
            session.execute(
                sa.text("""
                    UPDATE datasource_tables
                    SET connection_table_id = :connection_table_id
                    WHERE id = :id
                """),
                {
                    'id': dst.id,
                    'connection_table_id': conn_table_id,
                }
            )
        
        # 5. Migrate user_data_source_credentials to user_connection_credentials
        user_creds = session.execute(
            sa.text("""
                SELECT id, data_source_id, user_id, organization_id, auth_mode,
                       encrypted_credentials, is_active, is_primary, last_used_at,
                       expires_at, metadata_json, created_at, updated_at
                FROM user_data_source_credentials
                WHERE deleted_at IS NULL
            """)
        ).fetchall()
        
        for uc in user_creds:
            if uc.data_source_id not in ds_to_conn:
                continue
                
            conn_id = ds_to_conn[uc.data_source_id]
            new_id = str(uuid.uuid4())
            
            session.execute(
                sa.text("""
                    INSERT INTO user_connection_credentials (
                        id, connection_id, user_id, organization_id, auth_mode,
                        encrypted_credentials, is_active, is_primary, last_used_at,
                        expires_at, metadata_json, created_at, updated_at
                    ) VALUES (
                        :id, :connection_id, :user_id, :organization_id, :auth_mode,
                        :encrypted_credentials, :is_active, :is_primary, :last_used_at,
                        :expires_at, :metadata_json, :created_at, :updated_at
                    )
                """),
                {
                    'id': new_id,
                    'connection_id': conn_id,
                    'user_id': uc.user_id,
                    'organization_id': uc.organization_id,
                    'auth_mode': uc.auth_mode,
                    'encrypted_credentials': uc.encrypted_credentials,
                    'is_active': uc.is_active,
                    'is_primary': uc.is_primary,
                    'last_used_at': uc.last_used_at,
                    'expires_at': uc.expires_at,
                    'metadata_json': uc.metadata_json,
                    'created_at': uc.created_at,
                    'updated_at': uc.updated_at,
                }
            )
        
        # 6. Migrate user_data_source_tables to user_connection_tables
        # First build a map from (datasource_id, table_name) to connection_table_id
        conn_table_map = {}
        for row in session.execute(
            sa.text("""
                SELECT ct.id as conn_table_id, ct.name as table_name, ct.connection_id,
                       dc.data_source_id
                FROM connection_tables ct
                JOIN domain_connection dc ON ct.connection_id = dc.connection_id
            """)
        ).fetchall():
            conn_table_map[(row.data_source_id, row.table_name)] = (row.connection_id, row.conn_table_id)
        
        user_tables = session.execute(
            sa.text("""
                SELECT id, data_source_id, user_id, table_name, data_source_table_id,
                       is_accessible, status, metadata_json, created_at, updated_at
                FROM user_data_source_tables
                WHERE deleted_at IS NULL
            """)
        ).fetchall()
        
        for ut in user_tables:
            if ut.data_source_id not in ds_to_conn:
                continue
            
            conn_id = ds_to_conn[ut.data_source_id]
            conn_table_id = None
            if (ut.data_source_id, ut.table_name) in conn_table_map:
                _, conn_table_id = conn_table_map[(ut.data_source_id, ut.table_name)]
            
            new_id = str(uuid.uuid4())
            
            session.execute(
                sa.text("""
                    INSERT INTO user_connection_tables (
                        id, connection_id, user_id, table_name, connection_table_id,
                        is_accessible, status, metadata_json, created_at, updated_at
                    ) VALUES (
                        :id, :connection_id, :user_id, :table_name, :connection_table_id,
                        :is_accessible, :status, :metadata_json, :created_at, :updated_at
                    )
                """),
                {
                    'id': new_id,
                    'connection_id': conn_id,
                    'user_id': ut.user_id,
                    'table_name': ut.table_name,
                    'connection_table_id': conn_table_id,
                    'is_accessible': ut.is_accessible,
                    'status': ut.status,
                    'metadata_json': ut.metadata_json,
                    'created_at': ut.created_at,
                    'updated_at': ut.updated_at,
                }
            )
        
        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()
    
    # 7. Drop legacy columns from data_sources (data is now in connections)
    with op.batch_alter_table('data_sources', schema=None) as batch_op:
        batch_op.drop_column('type')
        batch_op.drop_column('config')
        batch_op.drop_column('credentials')
        batch_op.drop_column('auth_policy')
        batch_op.drop_column('allowed_user_auth_modes')


def downgrade() -> None:
    # First, recreate legacy columns on data_sources
    with op.batch_alter_table('data_sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('type', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('config', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('credentials', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('auth_policy', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('allowed_user_auth_modes', sa.JSON(), nullable=True))
    
    bind = op.get_bind()
    session = Session(bind=bind)
    
    try:
        # Restore data from connections back to data_sources
        session.execute(
            sa.text("""
                UPDATE data_sources
                SET type = c.type, 
                    config = c.config, 
                    credentials = c.credentials,
                    auth_policy = c.auth_policy, 
                    allowed_user_auth_modes = c.allowed_user_auth_modes
                FROM connections c
                JOIN domain_connection dc ON dc.connection_id = c.id
                WHERE dc.data_source_id = data_sources.id
            """)
        )
        
        # Clear connection_table_id links from datasource_tables
        session.execute(
            sa.text("UPDATE datasource_tables SET connection_table_id = NULL")
        )
        
        # Delete migrated data from new tables (preserving schema)
        session.execute(sa.text("DELETE FROM user_connection_columns"))
        session.execute(sa.text("DELETE FROM user_connection_tables"))
        session.execute(sa.text("DELETE FROM user_connection_credentials"))
        session.execute(sa.text("DELETE FROM domain_connection"))
        session.execute(sa.text("DELETE FROM connection_tables"))
        session.execute(sa.text("DELETE FROM connections"))
        
        session.commit()
    except Exception as e:
        session.rollback()
        raise e
    finally:
        session.close()

