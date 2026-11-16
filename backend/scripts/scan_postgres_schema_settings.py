"""Report saved Postgres connections whose schema setting the read refuses.

An agent query on a `postgresql` connection checks each item of the
connection's `schema` setting before it sends anything
(`postgresql_client.checked_search_path`). Saving a connection applies the
same rule when the setting is new or changed; a setting saved before that
check keeps working for indexing and discovery until an admin edits it, but
its agent queries fail. Run this before a release to find them.

Reads the app database only (deleted connections are skipped). The URL
comes from the app's own database settings, as for the app itself:
`BOW_DATABASE_URL`, or the database URL in the app config. Before scanning
it names, on stderr, the database's dialect and name (for SQLite, the
file's base name only), never its host, user or password. Prints one line
per refused connection: its id, its organization id and the kind of rule
that failed, or `unreadable config` when the config cannot be decoded. It
never prints the setting or any other config value.

Only the connection config is scanned. Neither a connection's own
credentials nor members' per-user credentials are: they are encrypted.
A `schema` in a connection's credentials is refused when it is saved. A
member's credentials on a `postgresql`, `mssql` or `neo4j` connection are
kept to the credentials model's fields when saved, and filtered to them
when read, so a member's `schema` never reaches the client.

    BOW_DATABASE_URL=... python scripts/scan_postgres_schema_settings.py

Exit 0 when nothing is reported, 1 when something is, 2 on any error (the
app's code or settings failing to load, the database unreachable), with one
error line on stderr (after the banner, once the settings have loaded).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import text

UNREADABLE = "unreadable config"


def _config(raw) -> Optional[dict]:
    """The config as a dict, or None when it cannot be decoded. The
    services store it as a JSON string inside the JSON column, sometimes
    encoded twice."""
    if raw is None:
        return {}
    value = raw
    for _ in range(3):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


def scan(engine) -> List[Tuple[str, str, str]]:
    """(connection id, organization id, kind of rule) for each saved
    `postgresql` connection whose schema setting has a refused item or
    whose config cannot be read."""
    from app.data_sources.clients.postgresql_client import schema_item_problem, schema_items

    found = []
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.exec_driver_sql("SET TRANSACTION READ ONLY")
        rows = conn.execute(text(
            "SELECT id, organization_id, config FROM connections"
            " WHERE type = 'postgresql' AND deleted_at IS NULL ORDER BY id"
        )).fetchall()
        conn.rollback()
    for conn_id, org_id, raw in rows:
        config = _config(raw)
        if config is None:
            found.append((str(conn_id), str(org_id), UNREADABLE))
            continue
        for item in schema_items(config.get("schema")):
            problem = schema_item_problem(item)
            if problem:
                found.append((str(conn_id), str(org_id), problem))
                break
    return found


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.parse_args(argv)

    try:
        # Inside the error handling: loading the app's code and settings can
        # fail too, and that is one line and exit 2 as well.
        import app.data_sources.clients.postgresql_client  # noqa: F401
        from app.settings.database import create_database_engine
        engine = create_database_engine()
        # Which database, never where or as whom (for SQLite, the file's
        # base name: its path is where).
        name = engine.url.database
        if engine.dialect.name == "sqlite" and name:
            name = Path(name).name
        print(f"scanning {engine.dialect.name} database {name}", file=sys.stderr)
        try:
            found = scan(engine)
        finally:
            engine.dispose()
    except Exception as e:
        # The type only: a driver's message can hold the URL or config values.
        print(f"scan failed: {type(e).__name__}", file=sys.stderr)
        return 2

    for conn_id, org_id, problem in found:
        print(f"connection_id={conn_id} organization_id={org_id} problem={problem}")
    if found:
        print(f"{len(found)} Postgres connection(s) have a schema setting that agent queries refuse "
              "or a config the scan cannot read; an admin must edit each one's schema setting.")
        return 1
    print("No Postgres connection has a schema setting that agent queries refuse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
