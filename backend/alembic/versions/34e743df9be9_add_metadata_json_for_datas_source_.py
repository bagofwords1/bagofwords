"""add metadata json for datas source tables

Revision ID: 34e743df9be9
Revises: 9a4893c8b0ee
Create Date: 2025-09-21 11:19:47.715137

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '34e743df9be9'
down_revision: Union[str, None] = '9a4893c8b0ee'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
 

    with op.batch_alter_table('datasource_tables', schema=None) as batch_op:
        batch_op.add_column(sa.Column('metadata_json', sa.JSON(), nullable=True))

    # ### end Alembic commands ###


def downgrade() -> None:

    with op.batch_alter_table('datasource_tables', schema=None) as batch_op:
        batch_op.drop_column('metadata_json')

    # ### end Alembic commands ###
