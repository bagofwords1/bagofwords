"""Serialize catalog publication per connection, after network discovery ends."""
from sqlalchemy import select, text
from app.models.connection import Connection


async def lock_connection_catalog(db, connection):
    if db.bind.dialect.name == "sqlite":
        # SQLite has no row locks. Take the writer lock without changing metadata.
        await db.execute(text("UPDATE connections SET id = id WHERE id = :id"),
                         {"id": str(connection.id)})
    else:
        await db.execute(select(Connection.id).where(
            Connection.id == str(connection.id)).with_for_update())
