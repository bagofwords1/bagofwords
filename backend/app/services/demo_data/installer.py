"""Install a generated demo dataset: SQLite file -> Connection -> agents.

The connection is created directly (like the bundled demos in
DemoDataSourceService) because no credentials or user-chosen config are
involved: the file path is chosen here, under ``uploads/demo_data/<org>/``,
never by the model. Agents go through the same DataSourceService path as the
``create_agent`` tool, so the license agent cap, name uniqueness and the
creator's ``manage`` grant all apply unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.connection import Connection
from app.models.connection_table import ConnectionTable
from app.models.data_source import DataSource
from app.models.datasource_table import DataSourceTable
from app.models.organization import Organization
from app.models.user import User
from app.schemas.demo_dataset_schema import DemoAgentSuggestion, DemoDatasetSpec, table_map

logger = logging.getLogger(__name__)

DEMO_DATA_ROOT = os.path.join("uploads", "demo_data")
# Marker keys in Connection.config. DEMO_ID_KEY matches the bundled demos so
# both kinds read as "demo" everywhere that already checks it.
DEMO_ID_KEY = "demo_id"
GENERATED_KEY = "demo_generated"
GENERATED_PREFIX = "generated:"
MAX_GENERATED_PER_ORG = 10


def dataset_path(organization_id: str, dataset_id: str) -> str:
    return os.path.join(DEMO_DATA_ROOT, str(organization_id), f"{dataset_id}.sqlite")


def is_generated_config(config) -> bool:
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            return False
    return bool(isinstance(config, dict) and config.get(GENERATED_KEY))


def remove_generated_file(config) -> None:
    """Delete a generated dataset's file once its connection is gone. Only
    ever touches files inside DEMO_DATA_ROOT."""
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except Exception:
            return
    if not is_generated_config(config):
        return
    path = (config or {}).get("database")
    if not path:
        return
    root = os.path.realpath(DEMO_DATA_ROOT)
    real = os.path.realpath(path)
    if not real.startswith(root + os.sep):
        logger.warning("demo dataset: refusing to delete file outside %s: %s", root, path)
        return
    try:
        os.remove(real)
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.warning("demo dataset: failed to delete %s: %s", real, e)


def emoji_token(value: Optional[str]) -> Optional[str]:
    """'💰' / 'emoji:💰' -> 'emoji:💰'. Keeps the first emoji grapheme (base +
    modifiers/ZWJ sequence); anything that isn't an emoji yields None so the
    agent falls back to its connector icon."""
    if not value:
        return None
    s = value.strip()
    if s.startswith("emoji:"):
        s = s[len("emoji:"):]
    if not s:
        return None
    out = s[0]
    if ord(out) < 0x2000:
        return None
    i = 1
    while i < len(s):
        ch = s[i]
        cp = ord(ch)
        if cp in (0x200D, 0xFE0F, 0xFE0E) or 0x1F3FB <= cp <= 0x1F3FF or unicodedata.combining(ch):
            out += ch
            if cp == 0x200D and i + 1 < len(s):
                out += s[i + 1]
                i += 1
            i += 1
            continue
        break
    return f"emoji:{out}"


@dataclass
class InstalledAgent:
    data_source_id: str
    name: str
    requested_name: str
    icon: Optional[str]
    active_tables: List[str]
    instructions_created: int = 0


@dataclass
class InstallResult:
    connection_id: str
    connection_name: str
    agents: List[InstalledAgent] = field(default_factory=list)
    skipped_agents: List[Dict[str, str]] = field(default_factory=list)


async def count_generated(db: AsyncSession, organization_id: str) -> int:
    rows = (await db.execute(
        select(Connection.config).where(Connection.organization_id == str(organization_id))
    )).scalars().all()
    return sum(1 for cfg in rows if is_generated_config(cfg))


async def _unique_connection_name(db: AsyncSession, organization_id: str, name: str) -> str:
    existing = set((await db.execute(
        select(Connection.name).where(Connection.organization_id == str(organization_id))
    )).scalars().all())
    if name not in existing:
        return name
    for i in range(2, 100):
        cand = f"{name} ({i})"
        if cand not in existing:
            return cand
    return f"{name} ({uuid.uuid4().hex[:6]})"


async def create_connection(
    db: AsyncSession,
    organization: Organization,
    user: User,
    spec: DemoDatasetSpec,
    *,
    dataset_id: str,
    path: str,
    icon: Optional[str] = None,
    generator_code: Optional[Dict[str, str]] = None,
) -> Connection:
    from app.services.connection_service import ConnectionService

    config = {
        "database": path,
        DEMO_ID_KEY: f"{GENERATED_PREFIX}{dataset_id}",
        GENERATED_KEY: True,
        # The approved spec, minus agents: enough to regenerate or explain the data.
        "demo_spec": spec.model_dump(mode="json", exclude={"agents"}),
    }
    if generator_code:
        # The code that produced each table: audits the data and, with the
        # spec's seed, rebuilds the same rows.
        config["demo_generator_code"] = generator_code
    if icon:
        config["icon"] = icon
    connection = Connection(
        name=await _unique_connection_name(db, str(organization.id), spec.name.strip()),
        type="sqlite",
        config=json.dumps(config),
        organization_id=str(organization.id),
        is_active=True,
        auth_policy="system_only",
    )
    db.add(connection)
    await db.commit()
    await db.refresh(connection)

    await ConnectionService().refresh_schema(db=db, connection=connection, current_user=user)
    await _apply_descriptions(db, connection, spec)
    return connection


async def _apply_descriptions(db: AsyncSession, connection: Connection, spec: DemoDatasetSpec) -> None:
    """Carry the spec's table/column descriptions into the catalog — SQLite
    has no comments, and they are what make the schema legible to the agent."""
    tmap = table_map(spec)
    rows = (await db.execute(
        select(ConnectionTable).where(ConnectionTable.connection_id == str(connection.id))
    )).scalars().all()
    for row in rows:
        t = tmap.get(row.name)
        if t is None:
            continue
        row.description = t.description
        cdesc = {c.name: c.description for c in t.columns if c.description}
        cols = []
        for col in (row.columns or []):
            col = dict(col)
            if col.get("name") in cdesc:
                col["description"] = cdesc[col["name"]]
            cols.append(col)
        row.columns = cols
        db.add(row)
    await db.commit()


async def create_agents(
    db: AsyncSession,
    organization: Organization,
    user: User,
    connection: Connection,
    agents: List[DemoAgentSuggestion],
    *,
    report=None,
) -> InstallResult:
    from app.schemas.data_source_schema import DataSourceCreate
    from app.services.data_source_service import DataSourceService
    from app.core.permission_resolver import invalidate_rbac_memo

    result = InstallResult(connection_id=str(connection.id), connection_name=connection.name)
    service = DataSourceService()
    taken = {n.lower() for n in (await db.execute(
        select(DataSource.name).where(DataSource.organization_id == str(organization.id))
    )).scalars().all() if n}
    for agent in agents:
        # A re-run of the same demo should still produce agents, not a 409.
        name = agent.name.strip()
        if name.lower() in taken:
            name = next(f"{name} ({i})" for i in range(2, 1000) if f"{name} ({i})".lower() not in taken)
        taken.add(name.lower())
        try:
            created = await service.create_data_source(db, organization, user, DataSourceCreate(
                name=name,
                connection_ids=[str(connection.id)],
                is_public=False,
                use_llm_sync=False,
            ))
        except HTTPException as he:
            reason = {409: "name_taken", 402: "limit_reached", 403: "permission_denied"}.get(he.status_code, "rejected")
            result.skipped_agents.append({"name": agent.name, "reason": reason, "detail": str(he.detail)})
            continue
        except Exception as e:  # keep going: the connection is already useful
            logger.warning("demo dataset: create agent %s failed: %s", agent.name, e)
            try:
                await db.rollback()
            except Exception:
                pass
            result.skipped_agents.append({"name": agent.name, "reason": "failed", "detail": str(e)[:300]})
            continue
        invalidate_rbac_memo(db, str(user.id), str(organization.id))

        ds = (await db.execute(
            select(DataSource).options(selectinload(DataSource.connections)).where(DataSource.id == str(created.id))
        )).scalar_one()
        icon = emoji_token(agent.icon)
        ds.description = (agent.description or "").strip() or None
        ds.icon = icon
        ds.conversation_starters = [s for s in agent.conversation_starters if s and s.strip()] or None
        db.add(ds)
        await db.commit()

        # Activate exactly the agent's tables.
        wanted = {t.lower() for t in agent.tables}
        t_rows = (await db.execute(
            select(DataSourceTable).where(DataSourceTable.datasource_id == str(ds.id))
        )).scalars().all()
        activate = [str(r.id) for r in t_rows if (r.name or "").lower() in wanted]
        deactivate = [str(r.id) for r in t_rows if (r.name or "").lower() not in wanted]
        if activate or deactivate:
            await service.update_tables_status_delta(
                db, str(ds.id), organization, activate=activate, deactivate=deactivate, current_user=user,
            )
        active_names = sorted(r.name for r in t_rows if (r.name or "").lower() in wanted)

        n_instr = await _create_instructions(db, organization, user, ds, agent.instructions)

        if report is not None:
            try:
                if str(ds.id) not in {str(d.id) for d in (report.data_sources or [])}:
                    report.data_sources.append(ds)
                    db.add(report)
                    await db.commit()
            except Exception as e:
                logger.warning("demo dataset: attach agent to report failed: %s", e)
                await db.rollback()

        result.agents.append(InstalledAgent(
            data_source_id=str(ds.id), name=ds.name, requested_name=agent.name, icon=icon,
            active_tables=active_names, instructions_created=n_instr,
        ))
    return result


async def _create_instructions(db, organization, user, ds: DataSource, texts: List[str]) -> int:
    texts = [t.strip() for t in (texts or []) if t and t.strip()]
    if not texts:
        return 0
    from app.schemas.instruction_schema import InstructionCreate
    from app.services.build_service import BuildService
    from app.services.instruction_service import InstructionService

    service = InstructionService()
    build_service = BuildService()
    build = None
    try:
        build = await build_service.get_or_create_draft_build(db, organization.id, source="user", user_id=user.id)
    except Exception as e:
        logger.warning("demo dataset: draft build failed: %s", e)
    created = 0
    for text in texts:
        try:
            await service.create_instruction(
                db=db,
                instruction_data=InstructionCreate(text=text, data_source_ids=[ds.id], category="general"),
                current_user=user,
                organization=organization,
                build=build,
                auto_finalize=False,
            )
            created += 1
        except Exception as e:
            logger.warning("demo dataset: instruction failed for %s: %s", ds.name, e)
    if build is not None and created:
        try:
            await build_service.submit_build(db, build.id)
            await build_service.approve_build(db, build.id, approved_by_user_id=user.id)
            await build_service.promote_build(db, build.id)
        except Exception as e:
            logger.warning("demo dataset: finalize build failed: %s", e)
    return created
