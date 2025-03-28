"""add text widget

Revision ID: 3422a3da5b2d
Revises: c6a5ec8e9206
Create Date: 2024-11-13 20:16:00.221450

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3422a3da5b2d'
down_revision: Union[str, None] = 'c6a5ec8e9206'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table('text_widgets',
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('x', sa.Integer(), nullable=False),
    sa.Column('y', sa.Integer(), nullable=False),
    sa.Column('width', sa.Integer(), nullable=False),
    sa.Column('height', sa.Integer(), nullable=False),
    sa.Column('content', sa.String(), nullable=False),
    sa.Column('report_id', sa.String(length=36), nullable=False),
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.Column('updated_at', sa.DateTime(), nullable=True),
    sa.Column('deleted_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('text_widgets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_text_widgets_id'), ['id'], unique=True)



def downgrade() -> None:
    with op.batch_alter_table('text_widgets', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_text_widgets_id'))

    op.drop_table('text_widgets')
    # ### end Alembic commands ###
