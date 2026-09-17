"""add template_key to scheduled_prompts

Revision ID: sptmpl01
Revises: ldapsecurity01
Create Date: 2026-09-06 00:00:00.000000

Stamps tasks created from a built-in template with the template's key, so the
Suggested-templates catalog can show per-user enabled state and the API can
lock template rows to schedule/notification edits only. Nullable: user-created
tasks stay unstamped. No unique constraint — idempotency is enforced at the
application level (mirroring the skill catalog's duplicate_count approach).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'sptmpl01'
down_revision: Union[str, None] = 'ldapsecurity01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('scheduled_prompts', sa.Column('template_key', sa.String(), nullable=True))
    op.create_index('ix_scheduled_prompts_template_key', 'scheduled_prompts', ['template_key'])


def downgrade() -> None:
    op.drop_index('ix_scheduled_prompts_template_key', table_name='scheduled_prompts')
    op.drop_column('scheduled_prompts', 'template_key')
