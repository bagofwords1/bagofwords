"""Single-use codes that hand an SSO-minted session token to the browser.

Replaces `redirect to /users/sign-in?access_token=<jwt>`. See
``app.models.login_exchange_code`` for why the JWT must not travel in a URL.
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.models.login_exchange_code import LoginExchangeCode

logger = logging.getLogger(__name__)

# The browser redeems the code on the very next request, so this only has to
# cover the redirect hop. Short by design: it caps how long a code scraped from
# a proxy log or browser history is worth anything.
CODE_LIFETIME = timedelta(seconds=60)


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


async def issue_login_code(user_id: str, access_token: str) -> str:
    """Persist ``access_token`` behind a fresh one-time code and return the code."""
    from app.dependencies import async_session_maker

    # Opportunistic housekeeping: the table only ever holds codes from the last
    # 60 seconds, so sweeping on issue keeps it from accumulating spent rows
    # without needing a scheduled job.
    await purge_expired_login_codes()

    code = secrets.token_urlsafe(32)
    async with async_session_maker() as db:
        db.add(
            LoginExchangeCode(
                code_hash=_hash(code),
                user_id=str(user_id),
                access_token=access_token,
                expires_at=datetime.utcnow() + CODE_LIFETIME,
            )
        )
        await db.commit()
    return code


async def redeem_login_code(code: str) -> str | None:
    """Consume ``code`` and return the session token, or None if it is not valid.

    Consumption is a conditional UPDATE, so two racing redemptions of the same
    code can never both come back with a token.
    """
    from app.dependencies import async_session_maker

    if not code:
        return None

    async with async_session_maker() as db:
        now = datetime.utcnow()
        result = await db.execute(
            update(LoginExchangeCode)
            .where(
                LoginExchangeCode.code_hash == _hash(code),
                LoginExchangeCode.consumed_at.is_(None),
                LoginExchangeCode.expires_at > now,
            )
            .values(consumed_at=now)
        )
        if result.rowcount != 1:
            await db.rollback()
            return None

        row = (
            await db.execute(
                select(LoginExchangeCode).where(
                    LoginExchangeCode.code_hash == _hash(code)
                )
            )
        ).scalar_one_or_none()
        await db.commit()

        if row is None:
            return None
        return row.access_token


async def purge_expired_login_codes() -> int:
    """Delete spent/expired rows. Best-effort housekeeping; never raises."""
    from sqlalchemy import delete, or_

    from app.dependencies import async_session_maker

    try:
        async with async_session_maker() as db:
            result = await db.execute(
                delete(LoginExchangeCode).where(
                    or_(
                        LoginExchangeCode.expires_at < datetime.utcnow(),
                        LoginExchangeCode.consumed_at.isnot(None),
                    )
                )
            )
            await db.commit()
            return result.rowcount or 0
    except Exception:
        logger.debug("purge_expired_login_codes failed", exc_info=True)
        return 0
