"""Serialize OAuth issuance and administrative session revocation per user."""
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.oauth_server import OAuthAccessToken, OAuthAuthorizationCode
from app.models.user import User


async def lock_user_sessions(db: AsyncSession, user_id: str) -> int:
    # A no-op UPDATE takes a row lock on Postgres and a write lock on SQLite.
    # Hold it only for local issuance/revocation, never during provider IO.
    await db.execute(
        update(User).where(User.id == str(user_id))
        .values(session_epoch=User.session_epoch)
        .execution_options(synchronize_session=False)
    )
    return await db.scalar(select(User.session_epoch).where(User.id == str(user_id)))


async def revoke_user_sessions(db: AsyncSession, user_id: str) -> None:
    """Revoke BOW sessions across organizations; leave upstream credentials intact."""
    await lock_user_sessions(db, user_id)
    await db.execute(
        update(User).where(User.id == str(user_id))
        .values(session_epoch=User.session_epoch + 1)
        .execution_options(synchronize_session=False)
    )
    now = datetime.utcnow()
    for model in (OAuthAuthorizationCode, OAuthAccessToken):
        await db.execute(
            update(model).where(model.user_id == str(user_id), model.deleted_at.is_(None))
            .values(deleted_at=now)
            .execution_options(synchronize_session=False)
        )
    await db.commit()
