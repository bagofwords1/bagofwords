"""rels migration

Revision ID: a2d893ef9090
Revises: 0c42ed0cd805
Create Date: 2024-08-09 21:32:40.292566

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from app.core.types import get_uuid_column

# revision identifiers, used by Alembic.
revision: str = 'a2d893ef9090'
down_revision: Union[str, None] = '0c42ed0cd805'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add widget_id to steps
    with op.batch_alter_table('steps') as batch_op:
        batch_op.add_column(sa.Column('widget_id', get_uuid_column(), nullable=True))
        batch_op.create_foreign_key('fk_steps_widget_id', 'widgets', ['widget_id'], ['id'])

    # Add report_id to widgets
    with op.batch_alter_table('widgets') as batch_op:
        batch_op.add_column(sa.Column('report_id', get_uuid_column(), nullable=True))
        batch_op.create_foreign_key('fk_widgets_report_id', 'reports', ['report_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('widgets') as batch_op:
        batch_op.drop_constraint('fk_widgets_report_id', type_='foreignkey')
        batch_op.drop_column('report_id')
    
    with op.batch_alter_table('steps') as batch_op:
        batch_op.drop_constraint('fk_steps_widget_id', type_='foreignkey')
        batch_op.drop_column('widget_id')
