"""merge SAML identities and artifact resources heads

Revision ID: mrgsamlart01
Revises: saml1001, artres01
Create Date: 2026-10-03 17:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'mrgsamlart01'
down_revision: Union[str, None] = ('saml1001', 'artres01')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
