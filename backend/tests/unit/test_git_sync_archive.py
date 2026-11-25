"""Archiving Git instructions whose source file left the repo.

A re-index archives every synced instruction whose file no longer exists and,
when a build is open, snapshots it into that build. The snapshot reads the
instruction's agents, labels and references, so the archive path must load
them the way the other sync paths do.

Hermetic: a throwaway SQLite DB; no real Git.
"""
import sys
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models.base import Base  # noqa: E402
from app.models.build_content import BuildContent  # noqa: E402
from app.models.instruction import Instruction  # noqa: E402
from app.models.instruction_build import InstructionBuild  # noqa: E402
from app.models.instruction_label import InstructionLabel  # noqa: E402
from app.models.instruction_version import InstructionVersion  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.services.instruction_sync_service import InstructionSyncService  # noqa: E402


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'archive.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(db, file_paths, labelled):
    org = Organization(name=f"Org {uuid.uuid4()}")
    db.add(org)
    await db.flush()
    label = InstructionLabel(name="Reviewed", organization_id=org.id)
    db.add(label)
    build = InstructionBuild(build_number=1, organization_id=org.id, source="git")
    db.add(build)
    for path in file_paths:
        instruction = Instruction(
            text=f"Rules from {path}",
            title=Path(path).stem,
            status="published",
            organization_id=org.id,
            source_type="git",
            source_sync_enabled=True,
            source_file_path=path,
        )
        if path in labelled:
            instruction.labels = [label]
        db.add(instruction)
    await db.commit()
    return org, label, build


@pytest.mark.db
@pytest.mark.asyncio
@pytest.mark.parametrize("labelled", [set(), {"docs/gone.md"}])
async def test_removed_file_is_archived_into_the_open_build(maker, labelled):
    async with maker() as db:
        org, label, build = await _seed(db, ["docs/gone.md", "docs/kept.md"], labelled)

    async with maker() as db:
        archived = await InstructionSyncService().archive_deleted_files(
            db, org.id, current_file_paths={"docs/kept.md"},
            path_prefix="docs/", build=await db.get(InstructionBuild, build.id),
            actor_id=str(uuid.uuid4()),
        )

    assert archived == 1
    async with maker() as db:
        rows = {i.source_file_path: i for i in (await db.scalars(select(Instruction))).all()}
        assert rows["docs/gone.md"].status == "archived"
        assert rows["docs/kept.md"].status == "published"

        content = (await db.scalars(select(BuildContent).where(BuildContent.build_id == build.id))).all()
        assert [c.instruction_id for c in content] == [rows["docs/gone.md"].id]
        version = await db.get(InstructionVersion, content[0].instruction_version_id)
        assert version.status == "archived"
        assert (version.label_ids or []) == ([label.id] if labelled else [])
