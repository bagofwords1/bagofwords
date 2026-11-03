"""Layout + prune behavior of GitService._write_build_to_repo (push to Git).

The push writes user instructions organized by agent (bow/{agent}/), splits
global vs shared, records the agents of a shared instruction in `agents:`
frontmatter, never lets two same-title instructions overwrite each other, and
prunes only files BOW itself wrote (via its manifest + the legacy category
folders) - leaving hand-authored notes and linked-git files alone.

Hermetic: a throwaway SQLite DB + a temp "repo" dir; no real Git.
"""
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

BACKEND_ROOT = Path(__file__).resolve().parents[2]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.models.base import Base  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.data_source import DataSource  # noqa: E402
from app.models.instruction import Instruction, instruction_data_source_association  # noqa: E402
from app.models.instruction_version import InstructionVersion  # noqa: E402
from app.models.instruction_build import InstructionBuild  # noqa: E402
from app.models.build_content import BuildContent  # noqa: E402
from app.services.git_service import GitService  # noqa: E402


@pytest_asyncio.fixture
async def maker(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'git.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _org(db):
    org = Organization(name=f"Org {uuid.uuid4()}")
    db.add(org)
    await db.flush()
    return org


async def _ds(db, org_id, name, deleted=False):
    ds = DataSource(name=name, organization_id=org_id)
    if deleted:
        ds.deleted_at = datetime.now(timezone.utc)
    db.add(ds)
    await db.flush()
    return ds


async def _build(db, org_id):
    build = InstructionBuild(build_number=1, organization_id=org_id, is_main=True)
    db.add(build)
    await db.flush()
    return build


async def _instruction(db, org_id, build, *, text, title=None, ds_ids=None,
                       source_type="user", source_sync_enabled=False, source_file_path=None):
    inst = Instruction(
        text=text, title=title, organization_id=org_id, status="published",
        source_type=source_type, source_sync_enabled=source_sync_enabled,
        source_file_path=source_file_path, category="general",
    )
    db.add(inst)
    await db.flush()
    ver = InstructionVersion(
        instruction_id=inst.id, version_number=1, text=text, title=title,
        content_hash="0" * 64, data_source_ids=list(ds_ids or []), references_json=[],
    )
    db.add(ver)
    await db.flush()
    db.add(BuildContent(build_id=build.id, instruction_id=inst.id, instruction_version_id=ver.id))
    await db.flush()
    return inst


def _repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


@pytest.mark.db
@pytest.mark.asyncio
async def test_single_global_and_shared_placement(maker, tmp_path):
    repo = _repo(tmp_path)
    async with maker() as db:
        org = await _org(db)
        sales = await _ds(db, org.id, "Sales")
        fabric = await _ds(db, org.id, "FABRIC")
        build = await _build(db, org.id)
        await _instruction(db, org.id, build, text="only sales", title="Sales Only", ds_ids=[sales.id])
        await _instruction(db, org.id, build, text="everyone", title="Global One", ds_ids=[])
        await _instruction(db, org.id, build, text="shared", title="Shared One", ds_ids=[sales.id, fabric.id])
        await db.commit()
        await GitService()._write_build_to_repo(db, build, str(repo))

    assert (repo / "bow/sales/sales-only.md").is_file()
    assert (repo / "bow/global/global-one.md").is_file()
    shared = repo / "bow/shared/shared-one.md"
    assert shared.is_file()
    # agents are recorded in valid YAML frontmatter, sorted
    front = yaml.safe_load(shared.read_text().split("---")[1])
    assert front["agents"] == ["FABRIC", "Sales"]


@pytest.mark.db
@pytest.mark.asyncio
async def test_uses_version_snapshot_not_live_scope(maker, tmp_path):
    """Placement follows the version's pinned data_source_ids, so re-scoping an
    instruction after the build was cut does not move an older build's file."""
    repo = _repo(tmp_path)
    async with maker() as db:
        org = await _org(db)
        sales = await _ds(db, org.id, "Sales")
        fabric = await _ds(db, org.id, "FABRIC")
        build = await _build(db, org.id)
        # Version snapshot pins [Sales]; the LIVE association points at FABRIC.
        inst = await _instruction(db, org.id, build, text="t", title="Pinned", ds_ids=[sales.id])
        await db.execute(
            instruction_data_source_association.insert().values(
                instruction_id=inst.id, data_source_id=fabric.id
            )
        )
        await db.commit()
        await GitService()._write_build_to_repo(db, build, str(repo))

    # Snapshot wins: file lands in Sales, not FABRIC.
    assert (repo / "bow/sales/pinned.md").is_file()
    assert not (repo / "bow/fabric").exists()


@pytest.mark.db
@pytest.mark.asyncio
async def test_soft_deleted_agent_falls_back_to_global(maker, tmp_path):
    repo = _repo(tmp_path)
    async with maker() as db:
        org = await _org(db)
        ghost = await _ds(db, org.id, "Ghost", deleted=True)
        build = await _build(db, org.id)
        await _instruction(db, org.id, build, text="orphaned", title="Orphan", ds_ids=[ghost.id])
        await db.commit()
        await GitService()._write_build_to_repo(db, build, str(repo))

    assert (repo / "bow/global/orphan.md").is_file()
    assert not (repo / "bow/ghost").exists()


@pytest.mark.db
@pytest.mark.asyncio
async def test_same_title_instructions_do_not_overwrite(maker, tmp_path):
    repo = _repo(tmp_path)
    async with maker() as db:
        org = await _org(db)
        build = await _build(db, org.id)
        await _instruction(db, org.id, build, text="first", title="Revenue Rules", ds_ids=[])
        await _instruction(db, org.id, build, text="second", title="Revenue Rules!", ds_ids=[])
        await db.commit()
        await GitService()._write_build_to_repo(db, build, str(repo))

    files = sorted((repo / "bow/global").glob("*.md"))
    assert len(files) == 2, [f.name for f in files]
    bodies = {f.read_text().split("\n\n")[-1] for f in files}
    assert {"first", "second"} <= bodies


@pytest.mark.db
@pytest.mark.asyncio
async def test_slug_colliding_agents_get_distinct_folders(maker, tmp_path):
    """Two differently-named agents whose names sanitize to the same slug
    (the org unique constraint forbids identical names) each get a distinct,
    id-suffixed folder - never the same one."""
    repo = _repo(tmp_path)
    async with maker() as db:
        org = await _org(db)
        a = await _ds(db, org.id, "Sales!")   # -> slug "sales"
        b = await _ds(db, org.id, "Sales?")   # -> slug "sales"
        build = await _build(db, org.id)
        await _instruction(db, org.id, build, text="a", title="A", ds_ids=[a.id])
        await _instruction(db, org.id, build, text="b", title="B", ds_ids=[b.id])
        await db.commit()
        await GitService()._write_build_to_repo(db, build, str(repo))

    dirs = sorted(p.name for p in (repo / "bow").iterdir() if p.is_dir())
    assert len(dirs) == 2, dirs
    assert all(d.startswith("sales-") for d in dirs), dirs


@pytest.mark.db
@pytest.mark.asyncio
async def test_prune_removes_only_bow_written_files(maker, tmp_path):
    repo = _repo(tmp_path)
    bow = repo / "bow"
    (bow / "general").mkdir(parents=True)
    (bow / "general/legacy.md").write_text("old category file")   # BOW-written, old layout
    (bow / "README.md").write_text("hand authored")               # hand-added at bow root
    (bow / "global").mkdir()
    (bow / "global/handnote.md").write_text("team note")          # hand-added inside a BOW folder
    (bow / "linked.md").write_text("git source of truth")         # linked-git file
    (bow / ".bow-manifest.json").write_text(
        json.dumps({"files": ["bow/general/legacy.md", "bow/linked.md"]})
    )

    async with maker() as db:
        org = await _org(db)
        build = await _build(db, org.id)
        await _instruction(db, org.id, build, text="fresh", title="New One", ds_ids=[])
        await _instruction(
            db, org.id, build, text="linked body", title="Linked",
            source_type="git", source_sync_enabled=True, source_file_path="bow/linked.md",
        )
        await db.commit()
        await GitService()._write_build_to_repo(db, build, str(repo))

    assert (bow / "global/new-one.md").is_file()        # written this push
    assert not (bow / "general/legacy.md").exists()     # BOW-written + stale -> pruned
    assert (bow / "README.md").exists()                 # hand-added -> kept
    assert (bow / "global/handnote.md").exists()        # hand-added in a BOW folder -> kept
    assert (bow / "linked.md").exists()                 # linked-git -> kept despite being in manifest

    manifest = json.loads((bow / ".bow-manifest.json").read_text())
    assert "bow/global/new-one.md" in manifest["files"]
    assert "bow/general/legacy.md" not in manifest["files"]
