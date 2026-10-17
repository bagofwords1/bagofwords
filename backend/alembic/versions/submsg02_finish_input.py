"""Keep exact finish input separate from normalized result."""
import sqlalchemy as sa

from alembic import op

revision = 'submsg02'
down_revision = 'submsg01'
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('sub_agent_runs', sa.Column('finish_input_json', sa.JSON(), nullable=True))

def downgrade():
    op.drop_column('sub_agent_runs', 'finish_input_json')
