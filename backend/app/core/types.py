import sqlalchemy
from alembic import context


def get_uuid_column():
    return sqlalchemy.String(36)