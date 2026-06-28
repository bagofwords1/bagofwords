"""Seed an organization with the default DCR integrations.

On org creation (and via backfill) we create, for each auto-seed catalog entry,
a "ghost" MCP connection (per-user OAuth / DCR — no admin client) and a public
integration agent that links it. Users then click "Connect" to sign in with
their own account; tools are discovered per-user after they connect.

Idempotent: skips an entry whose server_url is already connected for the org.
Never raises — a seeding failure must not break org creation.
"""
import json
import logging

from sqlalchemy import select

from app.models.connection import Connection
from app.schemas.connector_catalog import auto_seed_entries

logger = logging.getLogger(__name__)


def _conn_server_url(conn: Connection) -> str:
    try:
        cfg = json.loads(conn.config) if isinstance(conn.config, str) else (conn.config or {})
        return (cfg or {}).get("server_url") or ""
    except Exception:
        return ""


async def backfill_org_connectors(session_maker) -> int:
    """Seed default DCR integrations for EXISTING organizations that don't have
    them yet (idempotent). Runs once at startup in the leader worker. Picks each
    org's admin (or any member) as the owner of the seeded public agents."""
    from app.settings.config import settings
    if not getattr(settings.bow_config.features, "seed_default_connectors", True):
        return 0

    from app.models.organization import Organization
    from app.models.membership import Membership
    from app.models.user import User

    total = 0
    try:
        async with session_maker() as db:
            orgs = (await db.execute(select(Organization))).scalars().all()
            for org in orgs:
                try:
                    m = (await db.execute(
                        select(Membership).where(
                            Membership.organization_id == org.id,
                            Membership.role == "admin",
                            Membership.user_id.isnot(None),
                        ).limit(1)
                    )).scalar_one_or_none()
                    if not m:
                        m = (await db.execute(
                            select(Membership).where(
                                Membership.organization_id == org.id,
                                Membership.user_id.isnot(None),
                            ).limit(1)
                        )).scalar_one_or_none()
                    if not m:
                        continue
                    user = await db.get(User, m.user_id)
                    if not user:
                        continue
                    total += await seed_org_connectors(db, org, user)
                except Exception as e:
                    logger.warning("backfill_org_connectors: org %s failed: %s", getattr(org, "id", "?"), e)
    except Exception as e:
        logger.warning("backfill_org_connectors: aborted: %s", e)
    if total:
        logger.info("backfill_org_connectors: seeded %d connector agent(s) across existing orgs", total)
    return total


async def seed_org_connectors(db, organization, user) -> int:
    """Create ghost connections + public integration agents for auto-seed catalog
    entries the org doesn't already have. Returns the number seeded."""
    from app.settings.config import settings
    if not getattr(settings.bow_config.features, "seed_default_connectors", True):
        return 0

    from app.services.connection_service import ConnectionService
    from app.services.data_source_service import DataSourceService
    from app.schemas.data_source_schema import DataSourceCreate

    entries = auto_seed_entries()
    if not entries:
        return 0

    # Existing MCP server_urls for idempotency.
    existing_urls = set()
    try:
        rows = (await db.execute(
            select(Connection).where(
                Connection.organization_id == organization.id,
                Connection.type == "mcp",
            )
        )).scalars().all()
        existing_urls = {_conn_server_url(c) for c in rows}
    except Exception as e:
        logger.warning("seed_org_connectors: could not read existing connections: %s", e)

    conn_svc = ConnectionService()
    ds_svc = DataSourceService()
    seeded = 0
    for e in entries:
        if not e.server_url or e.server_url in existing_urls:
            continue
        try:
            conn = await conn_svc.create_connection(
                db, organization, user,
                name=e.title, type="mcp",
                # catalog_key lets the UI render the provider's icon (the
                # connection type is just "mcp" for all of them).
                config={"server_url": e.server_url, "transport": e.transport, "catalog_key": e.key},
                credentials={},
                auth_policy="user_required",
                allowed_user_auth_modes=["oauth"],
            )
            await ds_svc.create_data_source(
                db, organization, user,
                DataSourceCreate(
                    name=e.title,
                    connection_id=str(conn.id),
                    is_public=True,
                    generate_summary=False,
                    generate_conversation_starters=False,
                    generate_ai_rules=False,
                ),
            )
            existing_urls.add(e.server_url)
            seeded += 1
            logger.info("seed_org_connectors: seeded %s for org %s", e.key, organization.id)
        except Exception as ex:
            logger.warning("seed_org_connectors: failed to seed %s: %s", e.key, ex)
    return seeded
