"""Seed a SMALL, clean set of pending changes for the 'Pending changes' view
screenshot: a few well-named agents, each with a handful of instructions that
carry a live pending suggestion build with a MIX of sources (user / ai / git).
"""
import os, sys, uuid, asyncio, hashlib
os.environ.setdefault("BOW_DATABASE_URL", "sqlite:///db/app.db")
os.environ.setdefault("BOW_SMTP_PASSWORD", "dummy")
os.environ.setdefault("ANTHROPIC_API_KEY", "dummy")

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import select, insert
import app.models, pkgutil, importlib
for _, m, _ in pkgutil.iter_modules(app.models.__path__):
    if m != "application":
        importlib.import_module(f"app.models.{m}")
from app.models.user import User
from app.models.organization import Organization
from app.models.data_source import DataSource
from app.models.instruction import Instruction, instruction_data_source_association
from app.models.instruction_version import InstructionVersion
from app.models.instruction_build import InstructionBuild
from app.models.build_content import BuildContent
from app.models.data_source_membership import DataSourceMembership

def _hash(t): return hashlib.sha256(t.encode()).hexdigest()
def _uid(): return str(uuid.uuid4())

AGENTS = [
    ("Revenue Warehouse", "snowflake"),
    ("Product Analytics", "postgresql"),
    ("Marketing dbt", "bigquery"),
]
# (title, base, proposed, source, file_path)
INSTR = [
    ("Active accounts definition", "Count active accounts as accounts with a login in the last 30 days.",
     "Count active accounts as active, non-trial accounts with a login in the last 30 days. Cap lookback to trailing 24 months.", "ai", None),
    ("Revenue currency", "Report revenue in USD.", "Report revenue in USD, converting non-USD at month-end FX.", "user", None),
    ("Fiscal calendar", "The fiscal year starts in January.", "The fiscal year starts in February (4-4-5 calendar).", "git", "models/finance/fiscal_calendar.md"),
    ("Churn window", "Churn is measured over a 30-day window.", "Churn is measured over a 60-day rolling window.", "user", None),
    ("Trial exclusion", "Include trial accounts in MRR.", "Exclude trial accounts from MRR.", "ai", None),
    ("UTM normalization", "Lowercase all UTM sources.", "Lowercase and trim all UTM sources; map 'fb' to 'facebook'.", "git", "models/marketing/utm.md"),
]

async def main():
    engine = create_async_engine(os.environ["BOW_DATABASE_URL"].replace("sqlite://", "sqlite+aiosqlite://"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as db:
        user = (await db.execute(select(User).where(User.email == "sandbox@bow.dev"))).scalars().first()
        org = (await db.execute(select(Organization))).scalars().first()
        uid, org_id = str(user.id), str(org.id)
        mb = (await db.execute(select(InstructionBuild).where(
            InstructionBuild.organization_id == org_id, InstructionBuild.is_main == True))).scalars().first()
        mb_id = str(mb.id)
        next_bn = (await db.execute(select(InstructionBuild).where(
            InstructionBuild.organization_id == org_id))).scalars().all()
        bn = max((b.build_number or 0 for b in next_bn), default=0) + 1

        for ai_i, (aname, atype) in enumerate(AGENTS):
            aid = _uid()
            await db.execute(insert(DataSource.__table__), [{
                "id": aid, "name": f"[demo] {aname}", "type": atype, "is_active": True,
                "organization_id": org_id, "is_public": True, "publish_status": "published",
            }])
            await db.execute(insert(DataSourceMembership.__table__), [{
                "id": _uid(), "data_source_id": aid, "principal_type": "user", "principal_id": uid}])
            # 2 instructions per agent (slice the pool, wrap)
            for k in range(2):
                title, base, proposed, source, fpath = INSTR[(ai_i * 2 + k) % len(INSTR)]
                iid = _uid()
                await db.execute(insert(Instruction.__table__), [{
                    "id": iid, "text": base, "title": title, "status": "published",
                    "category": "general", "kind": "instruction", "user_id": uid,
                    "organization_id": org_id, "source_type": ("git" if source == "git" else "user"),
                    "source_file_path": fpath, "load_mode": "always",
                    "thumbs_up": 0, "is_seen": True, "can_user_toggle": True,
                }])
                await db.execute(insert(instruction_data_source_association), [
                    {"instruction_id": iid, "data_source_id": aid}])
                v1 = _uid()
                await db.execute(insert(InstructionVersion.__table__), [{
                    "id": v1, "instruction_id": iid, "version_number": 1, "text": base,
                    "title": title, "status": "published", "load_mode": "always",
                    "content_hash": _hash(base), "created_by_user_id": uid}])
                await db.execute(insert(BuildContent.__table__), [{
                    "id": _uid(), "build_id": mb_id, "instruction_id": iid, "instruction_version_id": v1}])
                v2 = _uid()
                await db.execute(insert(InstructionVersion.__table__), [{
                    "id": v2, "instruction_id": iid, "version_number": 2, "text": proposed,
                    "title": title, "status": "published", "load_mode": "always",
                    "content_hash": _hash(proposed), "created_by_user_id": uid}])
                sb = _uid()
                await db.execute(insert(InstructionBuild.__table__), [{
                    "id": sb, "build_number": bn, "status": "draft", "source": source,
                    "is_main": False, "organization_id": org_id, "base_build_id": mb_id,
                    "created_by_user_id": uid,
                    "description": f"{source} suggestion for {title}"}]); bn += 1
                await db.execute(insert(BuildContent.__table__), [{
                    "id": _uid(), "build_id": sb, "instruction_id": iid, "instruction_version_id": v2}])
        await db.commit()
        print("seeded demo pending agents")

if __name__ == "__main__":
    asyncio.run(main())
