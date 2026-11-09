"""Serialize Power BI catalog writes and repair historical duplicate identities."""
from app.core.migrations.powerbi_identity_v1 import repair_powerbi_identities


async def prepare_powerbi_catalog(db, connection):
    if connection.type != "powerbi":
        return
    from app.services.catalog_refresh_service import lock_connection_catalog
    await lock_connection_catalog(db, connection)
    await db.run_sync(lambda session: repair_powerbi_identities(session.connection(), str(connection.id)))
