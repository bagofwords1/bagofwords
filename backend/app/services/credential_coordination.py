"""Per-user auth coordination. No schema changes or database-wide locks.

PostgreSQL job locks live on a dedicated physical connection, independently of
ORM commits. Credential writers use a separate transaction lock, so Disconnect
can finish while Microsoft is responding. SQLite retains process-local job
admission only; PostgreSQL is required for cross-process guarantees.
"""

import asyncio
import hashlib
import threading
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

_local_mutex = threading.Lock()
_local_jobs: set[str] = set()


def _key(purpose, user_id):
    return int.from_bytes(hashlib.sha256(f"bow:auth:{purpose}:{user_id}".encode()).digest()[:8], "big", signed=True)


@asynccontextmanager
async def provisioning_lock(db, user_id):
    """Try once; a competing login/recovery owns this user's work already.

    Reentrant only for the same task/session, allowing recovery to hold the
    lock before refreshing its assertion and call the shared provisioning API.
    """
    user_id = str(user_id)
    owner = (user_id, asyncio.current_task())
    if db.info.get("obo_lock_owner") == owner:
        yield True
        return
    if db.bind.dialect.name != "postgresql":
        with _local_mutex:
            acquired = user_id not in _local_jobs
            if acquired:
                _local_jobs.add(user_id)
        try:
            if acquired:
                db.info["obo_lock_owner"] = owner
            yield acquired
        finally:
            if acquired:
                db.info.pop("obo_lock_owner", None)
                with _local_mutex:
                    _local_jobs.discard(user_id)
        return

    engine = db.bind.engine if isinstance(db.bind, AsyncConnection) else db.bind
    connection = await engine.connect()
    lock_key = _key("provision", user_id)
    acquired = False
    try:
        acquired = bool(await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_key}))
        # Session lock survives commits, and this connection never returns to
        # the pool until cleanup has released it.
        await connection.commit()
        if acquired:
            db.info["obo_lock_owner"] = owner
        yield acquired
    finally:
        db.info.pop("obo_lock_owner", None)

        async def cleanup():
            try:
                if acquired:
                    await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_key})
                    await connection.commit()
            finally:
                # Closing the physical connection also releases a lock if
                # cancellation happened while its acquisition was in flight.
                await connection.invalidate()
                await connection.close()

        cleanup_task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await cleanup_task
            raise


async def lock_credential_writes(db, user_id):
    """Acquire before reading credential rows; commit/rollback releases it.

    All manual OAuth/identity writers and automatic provisioning participate.
    Never hold this transaction lock across remote token or catalog calls.
    """
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _key("write", str(user_id))})


async def save_oauth_credentials(db, connection, user, tokens):
    """Explicit Connect wins over pending recovery and reuses a Disconnect row.

    Returns a snapshot for conditional restoration if remote verification fails.
    """
    from sqlalchemy import select

    from app.models.user_connection_credentials import UserConnectionCredentials
    from app.services.connection_oauth_service import parse_expires_at

    await lock_credential_writes(db, str(user.id))
    row = await db.scalar(
        select(UserConnectionCredentials)
        .where(
            UserConnectionCredentials.user_id == str(user.id),
            UserConnectionCredentials.connection_id == str(connection.id),
        )
        .order_by(
            UserConnectionCredentials.is_active.desc(),
            UserConnectionCredentials.is_primary.desc(),
            UserConnectionCredentials.updated_at.desc(),
        )
        .execution_options(populate_existing=True)
    )
    fields = ("encrypted_credentials", "expires_at", "auth_mode", "is_active", "is_primary", "metadata_json")
    prior = {name: getattr(row, name) for name in fields} if row else None
    if row and row.auth_mode == "oauth" and not tokens.get("refresh_token"):
        try:
            refresh = (row.decrypt_credentials() or {}).get("refresh_token")
        except Exception:
            refresh = None
        if refresh:
            tokens = {**tokens, "refresh_token": refresh}
    if row is None:
        row = UserConnectionCredentials(
            user_id=str(user.id), connection_id=str(connection.id), organization_id=str(connection.organization_id)
        )
    row.auth_mode = "oauth"
    row.is_active = row.is_primary = True
    metadata = dict(row.metadata_json or {})
    metadata.pop("auto_recovery_disabled", None)
    row.metadata_json = metadata
    row.encrypt_credentials(tokens)
    row.expires_at = parse_expires_at(tokens.get("expires_at"))
    db.add(row)
    await db.commit()
    return row, prior, row.encrypted_credentials


async def restore_failed_oauth_credentials(db, user_id, row_id, saved_blob, prior):
    """Do not undo a Disconnect or a newer Connect during remote verification."""
    from sqlalchemy import select

    from app.models.user_connection_credentials import UserConnectionCredentials

    await lock_credential_writes(db, user_id)
    row = await db.scalar(
        select(UserConnectionCredentials)
        .where(
            UserConnectionCredentials.id == row_id,
            UserConnectionCredentials.user_id == str(user_id),
        )
        .execution_options(populate_existing=True)
    )
    if row and row.encrypted_credentials == saved_blob:
        if prior is None:
            await db.delete(row)
        else:
            for name, value in prior.items():
                setattr(row, name, value)
    await db.commit()
