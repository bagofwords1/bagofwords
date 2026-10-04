"""Serialize Power BI catalog writes and repair historical duplicate identities."""
from sqlalchemy import select, text
from app.models.connection import Connection
from app.core.migrations.powerbi_identity_v1 import repair_powerbi_identities


async def prepare_powerbi_catalog(db, connection):
    if connection.type != "powerbi":
        return
    # Both shared and delegated refreshes can contribute catalog rows. Lock the
    # parent even for an empty catalog; locking discovered rows misses inserts.
    if db.bind.dialect.name == "sqlite":
        # SQLite has no row locks: take its writer lock before reading the
        # catalog. Do not change updated_at just to serialize discovery.
        await db.execute(text("UPDATE connections SET id = id WHERE id = :id"), {"id": str(connection.id)})
    else:
        await db.execute(select(Connection.id).where(Connection.id == str(connection.id)).with_for_update())
    await db.run_sync(lambda session: repair_powerbi_identities(session.connection(), str(connection.id)))
