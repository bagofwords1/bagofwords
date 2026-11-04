"""Fresh authorization for remote operations, including after a human wait."""

from sqlalchemy import select

from app.core.permission_resolver import invalidate_rbac_memo, user_can_access_data_source
from app.models.data_source import DataSource
from app.models.domain_connection import domain_connection
from app.models.membership import Membership
from app.models.user import User


async def remote_run_access(db, connection, user, agent_ids):
    if user is None or not agent_ids or connection.deleted_at is not None or not connection.is_active:
        return False
    member = (
        await db.execute(
            select(Membership.id)
            .join(User, User.id == Membership.user_id)
            .where(
                Membership.user_id == str(user.id),
                Membership.organization_id == str(connection.organization_id),
                Membership.deleted_at.is_(None),
                User.is_active.is_(True),
            )
        )
    ).first()
    if member is None:
        return False
    invalidate_rbac_memo(db, str(user.id), str(connection.organization_id))
    agents = (
        (
            await db.execute(
                select(DataSource)
                .join(domain_connection, domain_connection.c.data_source_id == DataSource.id)
                .where(
                    domain_connection.c.connection_id == str(connection.id),
                    DataSource.id.in_([str(i) for i in agent_ids]),
                    DataSource.organization_id == str(connection.organization_id),
                    DataSource.deleted_at.is_(None),
                )
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for agent in agents:
        if await user_can_access_data_source(db, str(user.id), str(connection.organization_id), agent):
            return True
    return False
